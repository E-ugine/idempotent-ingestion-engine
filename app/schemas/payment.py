from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

import pycountry
from pydantic import BaseModel, Field, field_validator


class PaymentMethod(str, Enum):
    CARD = "card"
    BANK_TRANSFER = "bank_transfer"
    WALLET = "wallet"


class PaymentCreateRequest(BaseModel):
    amount: Decimal = Field(..., gt=0)
    currency: str = Field(..., min_length=3, max_length=3)
    payment_method: PaymentMethod
    reference: str = Field(..., max_length=128)

    # account_id is intentionally absent: it is derived server-side from the
    # auth dependency, never accepted from the client.

    @field_validator("currency")
    @classmethod
    def validate_iso4217_currency(cls, value: str) -> str:
        normalized = value.upper()
        if pycountry.currencies.get(alpha_3=normalized) is None:
            raise ValueError(f"'{value}' is not a valid ISO 4217 currency code.")
        return normalized

    @field_validator("reference")
    @classmethod
    def validate_reference_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reference must not be empty or whitespace-only.")
        return stripped


class PaymentResponse(BaseModel):
    payment_id: UUID
    status: str
    amount: Decimal
    currency: str
    reference: str
    created_at: datetime
