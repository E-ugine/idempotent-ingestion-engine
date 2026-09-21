import hashlib
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.idempotency_key import IdempotencyKey
from app.schemas.payment import PaymentCreateRequest, PaymentResponse

logger = logging.getLogger(__name__)

_UNIQUE_VIOLATION = "23505"

# How long a "processing" row is trusted before being treated as abandoned.
# For instancethe, owning request crashed, or its PSP call never returned.
# This will need to grow once this integrates with
# a real PSP, where network latency alone could approach or exceed today's value

_STALE_PROCESSING_THRESHOLD_SECONDS = 30
_POLL_INTERVAL_SECONDS = 0.15

# pycountry's Currency object doesn't expose ISO 4217 minor-unit precision
# Only alpha_3/name/numeric, so it's hardcoded here. 
_ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG",
        "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF",
    }
)
_THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})
_DEFAULT_CURRENCY_DECIMAL_PLACES = 2


def _currency_decimal_places(currency: str) -> int:
    if currency in _ZERO_DECIMAL_CURRENCIES:
        return 0
    if currency in _THREE_DECIMAL_CURRENCIES:
        return 3
    return _DEFAULT_CURRENCY_DECIMAL_PLACES


def _quantize_amount_for_fingerprint(amount: Decimal, currency: str) -> Decimal:
    # Fingerprint-only normalization: Decimal("10.00") and Decimal("10.0")
    # must hash identically for the same currency. 
  
    exponent = Decimal(1).scaleb(-_currency_decimal_places(currency))
    return amount.quantize(exponent, rounding=ROUND_HALF_UP)


class IdempotencyConflictError(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


def _compute_request_fingerprint(account_id: str, payload: PaymentCreateRequest) -> str:
    quantized_amount = _quantize_amount_for_fingerprint(payload.amount, payload.currency)
    canonical = "|".join(
        [
            account_id,
            str(quantized_amount),
            payload.currency,
            payload.payment_method.value,
            payload.reference,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _simulate_charge(payload: PaymentCreateRequest) -> tuple[int, dict]:
    # Stands in for a real PSP call.
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


def _is_row_stale(updated_at: datetime) -> bool:

    age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
    return age_seconds >= _STALE_PROCESSING_THRESHOLD_SECONDS


def _raise_stale_processing_conflict() -> None:
    raise IdempotencyConflictError(
        "The original request with this idempotency key appears to have "
        "failed or stalled. Please retry with a new idempotency key."
    )


def _wait_for_existing_result(
    db: Session, account_id: str, idempotency_key: UUID
) -> tuple[int, dict]:

    key_str = str(idempotency_key)

    while True:
        db.commit()
        row = db.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.account_id == account_id,
                IdempotencyKey.idempotency_key == key_str,
            )
        ).scalar_one()

        if row.status == "completed":
            return row.response_status_code, row.response_body

        if _is_row_stale(row.updated_at):
            _raise_stale_processing_conflict()

        time.sleep(_POLL_INTERVAL_SECONDS)


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

            if _is_row_stale(existing.updated_at):
                _raise_stale_processing_conflict()
            return _wait_for_existing_result(db, account_id, idempotency_key)

        raise RuntimeError(f"Unexpected idempotency key status: {existing.status!r}")

    status_code, response_body = _simulate_charge(payload)
    new_row.status = "completed"
    new_row.response_status_code = status_code
    new_row.response_body = response_body
    db.commit()

    return status_code, response_body
