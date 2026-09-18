"""
Week 3 concurrency test suite.

Uses threading (not asyncio.gather()) for the genuinely-concurrent cases.
The route handler is a sync `def`, so FastAPI/Starlette dispatch each
request to a worker thread via anyio's threadpool regardless of how the
test issues the calls -- asyncio.gather() against TestClient would just
schedule coroutines that each block waiting on that threadpool, adding an
event loop in between for no benefit. Real OS threads, released together
via threading.Barrier, are the more direct way to force two requests to
actually be in flight against the DB at the same instant.
"""

import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import app.services.payment_service as payment_service_module
from app.db.models.idempotency_key import IdempotencyKey
from app.schemas.payment import PaymentCreateRequest
from tests.conftest import PAYMENTS_URL


def test_fingerprint_mismatch_returns_409(client, account_id, auth_headers, valid_payload):
    """Same key, different payload -> second request must be rejected, not processed."""
    key = str(uuid4())
    headers = {**auth_headers(account_id), "Idempotency-Key": key}

    resp_a = client.post(PAYMENTS_URL, json=valid_payload, headers=headers)
    assert resp_a.status_code == 201

    mismatched_payload = {**valid_payload, "amount": "20.00"}
    resp_b = client.post(PAYMENTS_URL, json=mismatched_payload, headers=headers)

    assert resp_b.status_code == 409
    assert resp_b.json()["detail"] == (
        "A request with this idempotency key has already been submitted."
    )


def test_simultaneous_identical_requests_return_same_payment_id(
    client, account_id, auth_headers, valid_payload
):
    """
    Two threads send the identical (key, payload) released at the same
    instant via a Barrier. Only one can win the INSERT; the other must
    fall into the duplicate-handling path. Both responses will read back
    as 201 -- the duplicate replays the winner's *stored* response, it
    isn't a distinct "already exists" status -- so status code alone
    can't distinguish "fresh charge" from "replay". The actual proof is
    that both bodies -- payment_id included -- are identical: two
    independent fresh charges would each mint their own payment_id.
    """
    key = str(uuid4())
    headers = {**auth_headers(account_id), "Idempotency-Key": key}
    barrier = threading.Barrier(2)
    responses = {}

    def send(index):
        barrier.wait()
        responses[index] = client.post(PAYMENTS_URL, json=valid_payload, headers=headers)

    threads = [threading.Thread(target=send, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    resp_1, resp_2 = responses[0], responses[1]

    assert resp_1.status_code == 201
    assert resp_2.status_code == 201

    body_1, body_2 = resp_1.json(), resp_2.json()
    assert body_1["payment_id"] == body_2["payment_id"]
    assert body_1 == body_2


def test_concurrent_duplicate_waits_for_in_flight_original(
    client, account_id, auth_headers, valid_payload, monkeypatch
):
    """
    Request A's charge simulation is slowed down to create a real window
    where its idempotency-key row is committed as "processing" but not
    yet "completed". Request B, sent into that window, must poll and wait
    for A rather than erroring -- proving the polling path in
    _wait_for_existing_result actually engages under real timing, not
    just when _simulate_charge is instant.
    """
    key = str(uuid4())
    headers = {**auth_headers(account_id), "Idempotency-Key": key}

    original_simulate_charge = payment_service_module._simulate_charge
    first_commit_done = threading.Event()

    def slow_simulate_charge(payload):
        # process_payment only calls _simulate_charge after its first
        # commit (the "processing" row insert) has already succeeded --
        # this is called past the try/except around that commit. So
        # setting the event here, before sleeping, is exactly the point
        # where the row is guaranteed visible to a concurrent reader.
        first_commit_done.set()
        time.sleep(1.0)
        return original_simulate_charge(payload)

    monkeypatch.setattr(payment_service_module, "_simulate_charge", slow_simulate_charge)

    results = {}

    def send_first():
        results["a"] = client.post(PAYMENTS_URL, json=valid_payload, headers=headers)

    thread_a = threading.Thread(target=send_first)
    thread_a.start()

    assert first_commit_done.wait(timeout=5), "A's first commit never landed"

    start = time.monotonic()
    resp_b = client.post(PAYMENTS_URL, json=valid_payload, headers=headers)
    elapsed_b = time.monotonic() - start

    thread_a.join()
    resp_a = results["a"]

    assert resp_a.status_code == 201
    assert resp_b.status_code == 201
    assert resp_a.json()["payment_id"] == resp_b.json()["payment_id"]
    assert resp_a.json() == resp_b.json()

    # B can only have gotten this answer by polling until A's completion
    # commit landed -- a fast failure or a premature/empty read would
    # return well under this bound.
    assert elapsed_b >= 0.5


def test_stale_processing_row_returns_409(
    client, account_id, auth_headers, db_session, valid_payload
):
    """
    A "processing" row with no request behind it anymore (simulating a
    crash between the two commits) must be rejected once past the
    staleness threshold, not treated as healthy in-flight work and polled
    forever, and not silently reprocessed as a fresh charge.
    """
    key = str(uuid4())

    # Fingerprint must match what the real request below will compute, so
    # this exercises the staleness branch specifically, not the
    # fingerprint-mismatch branch.
    fingerprint = payment_service_module._compute_request_fingerprint(
        account_id, PaymentCreateRequest(**valid_payload)
    )
    stale_updated_at = datetime.now(timezone.utc) - timedelta(
        seconds=payment_service_module._STALE_PROCESSING_THRESHOLD_SECONDS + 5
    )

    db_session.add(
        IdempotencyKey(
            account_id=account_id,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            status="processing",
            updated_at=stale_updated_at,
        )
    )
    db_session.commit()

    headers = {**auth_headers(account_id), "Idempotency-Key": key}
    resp = client.post(PAYMENTS_URL, json=valid_payload, headers=headers)

    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "The original request with this idempotency key appears to have "
        "failed or stalled. Please retry with a new idempotency key."
    )
