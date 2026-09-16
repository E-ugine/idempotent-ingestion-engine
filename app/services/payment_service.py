import hashlib
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.db.models.idempotency_key import IdempotencyKey
from app.schemas.payment import PaymentCreateRequest, PaymentResponse

logger = logging.getLogger(__name__)

_LOCK_WAIT_TIMEOUT_SECONDS = 10
_UNIQUE_VIOLATION = "23505"
_LOCK_NOT_AVAILABLE = "55P03"


class IdempotencyConflictError(Exception):
    """Raised for any case the caller should surface as HTTP 409."""

    def __init__(self, detail: str, retry_after: int | None = None):
        self.detail = detail
        self.retry_after = retry_after
        super().__init__(detail)


def _compute_request_fingerprint(account_id: str, payload: PaymentCreateRequest) -> str:
    canonical = "|".join(
        [
            account_id,
            str(payload.amount),
            payload.currency,
            payload.payment_method.value,
            payload.reference,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _simulate_charge(payload: PaymentCreateRequest) -> tuple[int, dict]:
    """Stands in for a real PSP call. Out of scope for this project: always succeeds instantly."""
    response = PaymentResponse(
        payment_id=uuid4(),
        status="completed",
        amount=payload.amount,
        currency=payload.currency,
        reference=payload.reference,
        created_at=datetime.now(timezone.utc),
    )
    return 201, response.model_dump(mode="json")


def _is_unique_violation(exc: IntegrityError) -> bool:
    return getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION


def _is_lock_timeout(exc: OperationalError) -> bool:
    return getattr(exc.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE


def _wait_for_existing_result(
    db: Session, account_id: str, idempotency_key: UUID
) -> tuple[int, dict]:
    """
    The row is committed with status="processing" but the owning request
    hasn't finished yet. Block on its row lock (bounded) instead of polling;
    once it's released, the owning request has committed its final result.
    """
    try:
        db.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_WAIT_TIMEOUT_SECONDS}s'"))
        row = db.execute(
            select(IdempotencyKey)
            .where(
                IdempotencyKey.account_id == account_id,
                IdempotencyKey.idempotency_key == str(idempotency_key),
            )
            .with_for_update()
        ).scalar_one()
        status_code, response_body = row.response_status_code, row.response_body
        db.commit()
    except OperationalError as exc:
        db.rollback()
        if _is_lock_timeout(exc):
            raise IdempotencyConflictError(
                "A request with this idempotency key is already being processed. "
                "Please retry shortly.",
                retry_after=5,
            ) from exc
        raise

    return status_code, response_body


def process_payment(
    db: Session,
    account_id: str,
    idempotency_key: UUID,
    payload: PaymentCreateRequest,
) -> tuple[int, dict]:
    fingerprint = _compute_request_fingerprint(account_id, payload)
    key_str = str(idempotency_key)

    new_row = IdempotencyKey(
        account_id=account_id,
        idempotency_key=key_str,
        request_fingerprint=fingerprint,
        status="processing",
    )
    db.add(new_row)

    try:
        # This insert (and its immediate commit) IS the atomicity guard --
        # no existence check beforehand. Committing now, before the charge is
        # simulated, is what makes the "processing" row visible to a
        # concurrent duplicate so it can lock and wait on it below, rather
        # than racing an in-flight transaction it can't yet see.
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if not _is_unique_violation(exc):
            raise

        existing = db.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.account_id == account_id,
                IdempotencyKey.idempotency_key == key_str,
            )
        ).scalar_one()

        if existing.request_fingerprint != fingerprint:
            logger.warning(
                "Idempotency key reused with a mismatched request fingerprint "
                "(account_id=%s, idempotency_key=%s, timestamp=%s)",
                account_id,
                key_str,
                datetime.now(timezone.utc).isoformat(),
            )
            raise IdempotencyConflictError(
                "A request with this idempotency key has already been submitted."
            )

        if existing.status == "completed":
            return existing.response_status_code, existing.response_body

        if existing.status == "processing":
            # TODO: expiry-based handling -- whether a stale "processing" row
            # (e.g. the original request's process died) should eventually be
            # treated as abandoned and reprocessed is an open design decision,
            # not implemented here.
            return _wait_for_existing_result(db, account_id, idempotency_key)

        raise RuntimeError(f"Unexpected idempotency key status: {existing.status!r}")

    # Insert succeeded -- this request owns the key. Simulate the charge and
    # persist the outcome as a second commit.
    status_code, response_body = _simulate_charge(payload)
    new_row.status = "completed"
    new_row.response_status_code = status_code
    new_row.response_body = response_body
    db.commit()

    return status_code, response_body
