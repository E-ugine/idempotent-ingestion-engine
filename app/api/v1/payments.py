from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.dependencies import get_current_account_id
from app.schemas.payment import PaymentCreateRequest, PaymentResponse

router = APIRouter()


@router.post(
    "/payments",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_payment(
    payment: PaymentCreateRequest,
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    account_id: str = Depends(get_current_account_id),
) -> PaymentResponse:
    """
    Shell only: validates the request contract (body, Idempotency-Key
    header, auth) and resolves account_id. Idempotency handling,
    persistence, and charge processing land in a later pass.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Payment processing is not yet implemented.",
    )
