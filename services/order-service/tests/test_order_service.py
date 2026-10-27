from decimal import Decimal
from fastapi.testclient import TestClient
import sqlalchemy
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

_real_create_engine = sqlalchemy.create_engine
sqlalchemy.create_engine = lambda *args, **kwargs: _real_create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
try:
    import main
finally:
    sqlalchemy.create_engine = _real_create_engine


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"{}" if payload is None else b"payload"

    def json(self):
        return self._payload


@pytest.fixture()
def setup_client(monkeypatch):
    test_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TestSession = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    main.Base.metadata.create_all(test_engine)
    monkeypatch.setattr(main, "SessionLocal", TestSession)
    calls = []
    payment_available = {"value": True}
    payment_status = {"value": "pending"}

    class FakeHttpClient:
        def request(self, method, url, timeout=None, headers=None, **kwargs):
            calls.append((method, url, kwargs))
            if "/products/" in url and method == "GET":
                pid = url.rsplit("/", 1)[-1]
                return FakeResponse(200, {"id": pid, "name": "Test Product", "sku": "SKU-1", "price": "12.50", "status": "PUBLISHED"})
            if url.endswith("/inventory/reservations") and method == "POST":
                return FakeResponse(201, {"status": "RESERVED"})
            if url.endswith("/inventory/reservations/release") and method == "POST":
                return FakeResponse(200, {"status": "RELEASED"})
            if url.endswith("/payments/create-order") and method == "POST":
                if not payment_available["value"]:
                    return FakeResponse(503, {"detail": "unavailable"})
                body = kwargs.get("json", {})
                return FakeResponse(201, {"payment_id": 42, "order_id": body.get("order_id"), "amount": body.get("amount"), "currency": body.get("currency"), "razorpay_order_id": "rzp_order_123", "status": "pending", "razorpay_order": {"id": "rzp_order_123"}})
            if url.endswith("/payments/verify") and method == "POST":
                payment_status["value"] = "success"
                return FakeResponse(200, {"status": "success", "payment": {"id": 42, "status": "success"}})
            if "/payments/42" in url and method == "GET":
                return FakeResponse(200, {"id": 42, "status": payment_status["value"]})
            return FakeResponse(404, {"detail": "not found"})
        @property
        def is_closed(self):
            return False
        def close(self):
            pass

    fake_client_instance = FakeHttpClient()
    monkeypatch.setattr(main, "get_http_client", lambda: fake_client_instance)
    with TestClient(main.app) as client:
        yield client, calls, payment_available, payment_status
    test_engine.dispose()


def auth(user="user-1", role="CUSTOMER"):
    return {"X-Auth-User-Id": user, "X-Auth-User-Role": role}


def create_order(client, headers=None):
    return client.post("/orders", headers=auth() if headers is None else headers, json={"items": [{"product_id": "p-1", "quantity": 2}], "currency": "INR"})


def test_health_reports_database_up(setup_client):
    client, *_ = setup_client
    assert client.get("/health").json()["database"] == "UP"


def test_create_order_requires_identity(setup_client):
    client, *_ = setup_client
    assert create_order(client, headers={}).status_code == 401


def test_list_orders_requires_identity(setup_client):
    client, *_ = setup_client
    assert client.get("/orders").status_code == 401


def test_create_order_snapshots_price_reserves_stock_and_creates_payment(setup_client):
    client, calls, *_ = setup_client
    response = create_order(client)
    assert response.status_code == 201
    body = response.json()
    assert body["subtotal"] == "25.00"
    assert body["grand_total"] == "25.00"
    assert body["status"] == "PAYMENT_PENDING"
    assert body["payment"]["payment_id"] == "42"
    assert body["payment"]["razorpay_order_id"] == "rzp_order_123"
    assert any(url.endswith("/inventory/reservations") for _, url, _ in calls)
    assert any(url.endswith("/payments/create-order") for _, url, _ in calls)


def test_duplicate_product_lines_are_combined(setup_client):
    client, calls, *_ = setup_client
    response = client.post("/orders", headers=auth(), json={"items": [{"product_id": "p-1", "quantity": 1}, {"product_id": "p-1", "quantity": 2}]})
    assert response.status_code == 201
    reserve_call = next(c for c in calls if c[1].endswith("/inventory/reservations"))
    assert reserve_call[2]["json"]["items"] == [{"product_id": "p-1", "quantity": 3}]
    assert response.json()["subtotal"] == "37.50"


def test_stock_conflict_cancels_order(setup_client):
    client, calls, *_ = setup_client
    def conflict_request(method, url, timeout=None, headers=None, **kwargs):
        if url.endswith("/products/p-1"):
            return FakeResponse(200, {"id": "p-1", "name": "Test Product", "sku": "SKU-1", "price": "12.50", "status": "PUBLISHED"})
        if url.endswith("/inventory/reservations"):
            return FakeResponse(409, {"detail": "Insufficient stock"})
        return FakeResponse(404, {})
    import main as module
    original = module.httpx.request
    module.httpx.request = conflict_request
    try:
        response = create_order(client)
    finally:
        module.httpx.request = original
    assert response.status_code == 409


def test_payment_service_failure_leaves_order_retryable(setup_client):
    client, _, available, _ = setup_client
    available["value"] = False
    response = create_order(client)
    assert response.status_code == 201
    body = response.json()
    assert body["payment_setup"] == "RETRY_REQUIRED"
    assert body["status"] == "PAYMENT_PENDING"
    available["value"] = True
    retry = client.post(f"/orders/{body['id']}/payment", headers=auth())
    assert retry.status_code == 200
    assert retry.json()["payment"]["razorpay_order_id"] == "rzp_order_123"


def test_order_is_not_visible_to_another_customer(setup_client):
    client, *_ = setup_client
    body = create_order(client).json()
    response = client.get(f"/orders/{body['id']}", headers=auth("user-2"))
    assert response.status_code == 403


def test_owner_can_retrieve_order(setup_client):
    client, *_ = setup_client
    body = create_order(client).json()
    response = client.get(f"/orders/{body['id']}", headers=auth())
    assert response.status_code == 200
    assert response.json()["id"] == body["id"]


def test_valid_payment_verification_marks_order_paid(setup_client):
    client, *_ = setup_client
    body = create_order(client).json()
    response = client.post(f"/orders/{body['id']}/payment/verify", headers=auth(), json={"razorpay_order_id": "rzp_order_123", "razorpay_payment_id": "pay_123", "razorpay_signature": "signature"})
    assert response.status_code == 200
    assert response.json()["status"] == "PAID"


def test_cancel_releases_reservation(setup_client):
    client, calls, *_ = setup_client
    body = create_order(client).json()
    response = client.post(f"/orders/{body['id']}/cancel", headers=auth(), json={"reason": "customer request"})
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert any(url.endswith("/inventory/reservations/release") for _, url, _ in calls)


def test_cancel_rejected_after_payment_success(setup_client):
    client, _, _, payment_status = setup_client
    body = create_order(client).json()
    payment_status["value"] = "success"
    response = client.post(f"/orders/{body['id']}/cancel", headers=auth(), json={})
    assert response.status_code == 409
