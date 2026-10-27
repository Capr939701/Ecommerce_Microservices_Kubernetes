import importlib.util
from pathlib import Path

import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
spec = importlib.util.spec_from_file_location("product_main", MODULE_PATH)
product_main = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = product_main
_real_create_engine = sqlalchemy.create_engine
sqlalchemy.create_engine = lambda *args, **kwargs: _real_create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
try:
    spec.loader.exec_module(product_main)
finally:
    sqlalchemy.create_engine = _real_create_engine


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    product_main.engine = engine
    product_main.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    product_main.Base.metadata.drop_all(bind=engine)
    product_main.Base.metadata.create_all(bind=engine)
    with TestClient(product_main.app) as test_client:
        yield test_client
    product_main.Base.metadata.drop_all(bind=engine)
    engine.dispose()


ADMIN = {"x-auth-user-id": "admin-1", "x-auth-user-role": "ADMIN"}
CUSTOMER = {"x-auth-user-id": "customer-1", "x-auth-user-role": "CUSTOMER"}


def make_product(client, sku="SKU-001", stock=5):
    response = client.post("/products", headers=ADMIN, json={
        "sku": sku, "name": "Test Product", "description": "test",
        "price": "12.50", "initial_stock": stock, "status": "PUBLISHED",
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_health_reports_database_up(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "UP"


def test_product_create_and_inventory_are_created_together(client):
    product = make_product(client, stock=7)
    assert product["stock"] == 7
    assert product["stockStatus"] == "In Stock"
    inventory = client.get(f"/inventory/{product['id']}")
    assert inventory.status_code == 200
    assert inventory.json()["quantity"] == 7


def test_duplicate_sku_conflicts(client):
    make_product(client)
    response = client.post("/products", headers=ADMIN, json={
        "sku": "SKU-001", "name": "Another Product", "price": "2.00", "initial_stock": 0,
    })
    assert response.status_code == 409


def test_product_mutations_require_admin(client):
    response = client.post("/products", headers=CUSTOMER, json={
        "sku": "SKU-002", "name": "Customer Product", "price": "2.00", "initial_stock": 0,
    })
    assert response.status_code == 403


def test_public_catalog_hides_unpublished_products(client):
    product = make_product(client)
    client.patch(f"/products/{product['id']}", headers=ADMIN, json={"status": "UNPUBLISHED"})
    assert client.get("/products").json()["pagination"]["total"] == 0


def test_reservation_decrements_stock_and_is_idempotent(client):
    product = make_product(client, stock=5)
    payload = {"order_id": "ORDER-1", "items": [{"product_id": product["id"], "quantity": 2}]}
    first = client.post("/inventory/reservations", headers=CUSTOMER, json=payload)
    assert first.status_code == 201, first.text
    second = client.post("/inventory/reservations", headers=CUSTOMER, json=payload)
    assert second.status_code == 201, second.text
    assert second.json()["idempotent"] is True
    assert client.get(f"/inventory/{product['id']}").json()["quantity"] == 3


def test_insufficient_stock_does_not_decrement(client):
    product = make_product(client, stock=1)
    response = client.post("/inventory/reservations", headers=CUSTOMER, json={
        "order_id": "ORDER-2", "items": [{"product_id": product["id"], "quantity": 2}],
    })
    assert response.status_code == 409
    assert client.get(f"/inventory/{product['id']}").json()["quantity"] == 1


def test_release_restores_stock_only_once(client):
    product = make_product(client, stock=4)
    payload = {"order_id": "ORDER-3", "items": [{"product_id": product["id"], "quantity": 3}]}
    assert client.post("/inventory/reservations", headers=CUSTOMER, json=payload).status_code == 201
    release_payload = {"order_id": "ORDER-3"}
    first = client.post("/inventory/reservations/release", headers=CUSTOMER, json=release_payload)
    second = client.post("/inventory/reservations/release", headers=CUSTOMER, json=release_payload)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["idempotent"] is True
    assert client.get(f"/inventory/{product['id']}").json()["quantity"] == 4


def test_missing_product_returns_404(client):
    assert client.get("/products/no-such-product").status_code == 404


def test_unpublished_product_hidden_from_public(client):
    product = make_product(client)
    # Delete / unpublish product as admin
    res = client.delete(f"/products/{product['id']}", headers=ADMIN)
    assert res.status_code == 204
    # Public request returns 404
    assert client.get(f"/products/{product['id']}").status_code == 404
    # Admin request can still view it
    admin_res = client.get(f"/products/{product['id']}", headers=ADMIN)
    assert admin_res.status_code == 200
    assert admin_res.json()["status"] == "UNPUBLISHED"


def test_invalid_stock_value_is_rejected(client):
    response = client.post("/products", headers=ADMIN, json={
        "sku": "SKU-003", "name": "Invalid Stock", "price": "4.00", "initial_stock": -1,
    })
    assert response.status_code == 422
