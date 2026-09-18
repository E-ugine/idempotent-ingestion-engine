import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.base import SessionLocal
from app.main import app

PAYMENTS_URL = "/api/v1/payments"


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def db_session():
    """
    A DB session independent of the app's own per-request sessions (see
    app.dependencies.get_db) -- for direct test setup/assertions against
    the same database the app is actually using.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def clean_idempotency_keys_table():
    """
    These tests run against the real Postgres instance (docker-compose),
    not a mock -- the behavior under test (the unique constraint race,
    polling for another request's commit) can't be faithfully exercised
    against an in-memory/mocked DB. Truncate before each test so no test's
    rows leak into the next.
    """
    session = SessionLocal()
    try:
        session.execute(text("TRUNCATE TABLE idempotency_keys RESTART IDENTITY"))
        session.commit()
    finally:
        session.close()
    yield


@pytest.fixture
def account_id() -> str:
    return "acct-test-0001"


@pytest.fixture
def auth_headers():
    def _auth_headers(account_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {account_id}"}

    return _auth_headers


@pytest.fixture
def valid_payload() -> dict:
    return {
        "amount": "10.00",
        "currency": "usd",
        "payment_method": "card",
        "reference": "order-1",
    }
