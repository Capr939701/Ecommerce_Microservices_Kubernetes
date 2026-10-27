"""Offline tests for payment state and idempotency; no Razorpay network calls."""
import os
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["RAZORPAY_KEY_ID"] = "test_key"
os.environ["RAZORPAY_KEY_SECRET"] = "test_secret"
os.environ["DB_CONNECT_RETRIES"] = "1"
os.environ["DB_CONNECT_RETRY_DELAY_SECONDS"] = "0.1"


class SignatureVerificationError(Exception):
    pass


class FakeOrderAPI:
    def __init__(self):
        self.calls = 0

    def create(self, data):
        self.calls += 1
        return {"id": f"order_razorpay_{self.calls}", **data}


class FakeUtilityAPI:
    def verify_payment_signature(self, data):
        if data["razorpay_signature"] != "valid-signature":
            raise SignatureVerificationError("invalid signature")
        return True


class FakeClient:
    def __init__(self, auth):
        self.auth = auth
        self.order = FakeOrderAPI()
        self.utility = FakeUtilityAPI()


fake_razorpay = types.ModuleType("razorpay")
fake_razorpay.Client = FakeClient
fake_razorpay.errors = types.SimpleNamespace(SignatureVerificationError=SignatureVerificationError)
sys.modules["razorpay"] = fake_razorpay
sys.modules["razorpay.errors"] = fake_razorpay.errors

import pytest
import sqlalchemy
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
_real_create_engine = sqlalchemy.create_engine
sqlalchemy.create_engine = lambda *args, **kwargs: _real_create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
sys.modules.pop("main", None)
try:
    import main
finally:
    sqlalchemy.create_engine = _real_create_engine


@pytest.fixture(autouse=True)
def clean_db():
    main.Base.metadata.drop_all(bind=main.engine)
    main.Base.metadata.create_all(bind=main.engine)
    main.razorpay_client.order.calls = 0
    yield


@pytest.fixture
def client():
    with TestClient(main.app, headers={"X-Auth-User-Id": "test-user-1", "X-Auth-User-Role": "CUSTOMER"}) as test_client:
        yield test_client


def create_payment(client, order_id="shop-order-1"):
    response = client.post("/payments/create-order", json={
        "order_id": order_id,
        "amount": "25.50",
        "currency": "INR",
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_health_checks_database(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "connected"


def test_create_order_is_idempotent(client):
    first = create_payment(client)
    second = client.post("/payments/create-order", json={
        "order_id": "shop-order-1", "amount": "25.50", "currency": "INR"
    })
    assert second.status_code == 200
    assert second.json()["payment_id"] == first["payment_id"]
    assert main.razorpay_client.order.calls == 1


def test_duplicate_order_with_different_amount_conflicts(client):
    create_payment(client)
    response = client.post("/payments/create-order", json={
        "order_id": "shop-order-1", "amount": "26.00", "currency": "INR"
    })
    assert response.status_code == 409
    assert main.razorpay_client.order.calls == 1


def test_invalid_signature_does_not_mutate_payment(client):
    payment = create_payment(client)
    response = client.post("/payments/verify", json={
        "razorpay_order_id": payment["razorpay_order_id"],
        "razorpay_payment_id": "pay_test_1",
        "razorpay_signature": "invalid-signature",
    })
    assert response.status_code == 400
    stored = client.get(f"/payments/{payment['payment_id']}").json()
    assert stored["status"] == "pending"
    assert stored["razorpay_payment_id"] is None


def test_successful_payment_cannot_be_undone(client):
    payment = create_payment(client)
    valid = {
        "razorpay_order_id": payment["razorpay_order_id"],
        "razorpay_payment_id": "pay_test_2",
        "razorpay_signature": "valid-signature",
    }
    response = client.post("/payments/verify", json=valid)
    assert response.status_code == 200
    assert response.json()["status"] == "success"

    replay = {**valid, "razorpay_signature": "invalid-signature", "razorpay_payment_id": "pay_other"}
    response = client.post("/payments/verify", json=replay)
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["payment"]["razorpay_payment_id"] == "pay_test_2"


def test_get_unknown_payment_returns_404(client):
    response = client.get("/payments/99999")
    assert response.status_code == 404


def test_amount_must_be_positive(client):
    response = client.post("/payments/create-order", json={
        "order_id": "bad-amount", "amount": "0", "currency": "INR"
    })
    assert response.status_code == 422


def test_health_returns_503_when_database_is_unavailable(client, monkeypatch):
    class BrokenEngine:
        def connect(self):
            from sqlalchemy.exc import OperationalError
            raise OperationalError("SELECT 1", {}, RuntimeError("database offline"))

    monkeypatch.setattr(main, "engine", BrokenEngine())
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["detail"] == "Database unavailable"


def test_create_order_rejects_already_paid_order(client):
    payment = create_payment(client)
    verified = client.post("/payments/verify", json={
        "razorpay_order_id": payment["razorpay_order_id"],
        "razorpay_payment_id": "pay_test_paid",
        "razorpay_signature": "valid-signature",
    })
    assert verified.status_code == 200
    response = client.post("/payments/create-order", json={
        "order_id": "shop-order-1", "amount": "25.50", "currency": "INR"
    })
    assert response.status_code == 409


def test_failed_payment_allows_a_new_attempt(client):
    payment = create_payment(client)
    db = main.SessionLocal()
    try:
        record = db.query(main.PaymentRecord).filter_by(id=payment["payment_id"]).one()
        record.status = "failed"
        db.commit()
    finally:
        db.close()

    retry = client.post("/payments/create-order", json={
        "order_id": "shop-order-1", "amount": "25.50", "currency": "INR"
    })
    assert retry.status_code == 201
    assert retry.json()["payment_id"] != payment["payment_id"]
    assert main.razorpay_client.order.calls == 2


def test_unauthenticated_request_is_rejected():
    with TestClient(main.app) as unauth_client:
        response = unauth_client.post("/payments/create-order", json={
            "order_id": "shop-order-unauth", "amount": "10.00", "currency": "INR"
        })
        assert response.status_code == 401
        assert response.json()["detail"] == "Authenticated user identity is required"


def test_missing_razorpay_credentials_fails_startup(monkeypatch):
    monkeypatch.setattr(main, "RAZORPAY_KEY_ID", "")
    with pytest.raises(RuntimeError, match="RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be configured"):
        main.validate_configuration()
