from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.dependencies import get_current_account_id, get_db
from app.schemas.payment import PaymentCreateRequest, PaymentResponse
from app.services.payment_service import IdempotencyConflictError, process_payment

router = APIRouter()


@router.post(
    "/payments",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_payment(
    payment: PaymentCreateRequest,
    response: Response,
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    account_id: str = Depends(get_current_account_id),
    db: Session = Depends(get_db),
) -> PaymentResponse:
    try:
        status_code, response_body = process_payment(db, account_id, idempotency_key, payment)
    except IdempotencyConflictError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.detail,
            headers=headers,
        ) from exc

    response.status_code = status_code
    return response_body
