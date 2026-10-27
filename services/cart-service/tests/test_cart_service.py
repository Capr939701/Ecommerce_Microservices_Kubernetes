import importlib.util
import sys
from pathlib import Path

import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

MODULE = Path(__file__).resolve().parents[1] / "main.py"
spec = importlib.util.spec_from_file_location("cart_main", MODULE)
cart = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cart
_real_create_engine = sqlalchemy.create_engine
sqlalchemy.create_engine = lambda *args, **kwargs: _real_create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
try:
    spec.loader.exec_module(cart)
finally:
    sqlalchemy.create_engine = _real_create_engine

class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code, self._payload = status_code, payload
    def json(self): return self._payload

@pytest.fixture
def setup_client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    cart.engine = engine
    cart.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    cart.Base.metadata.drop_all(bind=engine)
    cart.Base.metadata.create_all(bind=engine)
    product = {"id": "p1", "sku": "SKU-1", "name": "Demo", "price": "12.50", "stock": 5, "status": "PUBLISHED"}
    calls = []
    class FakeHttpClient:
        def get(self, url, timeout=None, headers=None):
            calls.append(("GET", url, headers))
            return FakeResponse(200, product)
        def post(self, url, json=None, timeout=None, headers=None):
            calls.append(("POST", url, json, headers))
            return FakeResponse(201, {"id": "order-1", "status": "PAYMENT_PENDING", "items": json["items"]})
        @property
        def is_closed(self):
            return False
        def close(self):
            pass

    fake_client_instance = FakeHttpClient()
    monkeypatch.setattr(cart, "get_http_client", lambda: fake_client_instance)
    with TestClient(cart.app) as client:
        yield client, calls, product
    engine.dispose()

AUTH = {"X-Auth-User-Id": "user-1", "X-Auth-User-Role": "CUSTOMER"}

def test_cart_requires_gateway_identity(setup_client):
    client, _, _ = setup_client
    assert client.get("/cart").status_code == 401

def test_add_and_view_cart(setup_client):
    client, _, _ = setup_client
    r = client.post("/cart/items", headers=AUTH, json={"product_id":"p1", "quantity":2})
    assert r.status_code == 201
    body = client.get("/cart", headers=AUTH).json()
    assert body["items"][0]["quantity"] == 2
    assert body["total"] == "25.00"

def test_checkout_sends_order_and_clears_cart(setup_client):
    client, calls, _ = setup_client
    client.post("/cart/items", headers=AUTH, json={"product_id":"p1", "quantity":2})
    response = client.post("/cart/checkout", headers=AUTH, json={"currency":"INR"})
    assert response.status_code == 201
    assert any(call[0] == "POST" and call[1].endswith("/orders") for call in calls)
    assert client.get("/cart", headers=AUTH).json()["items"] == []

def test_cart_items_are_scoped_to_authenticated_user(setup_client):
    client, _, _ = setup_client
    client.post("/cart/items", headers=AUTH, json={"product_id":"p1", "quantity":1})
    other = client.get("/cart", headers={"X-Auth-User-Id":"user-2", "X-Auth-User-Role":"CUSTOMER"})
    assert other.json()["items"] == []
