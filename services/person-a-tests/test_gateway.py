import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gateway_module", ROOT / "api-gateway" / "main.py")
gateway = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gateway
spec.loader.exec_module(gateway)


def test_route_table_maps_auth_payment_and_cart():
    assert gateway.service_for("/auth/login") == gateway.USER_AUTH_URL
    assert gateway.service_for("/payments/create-order") == gateway.PAYMENT_SERVICE_URL
    assert gateway.service_for("/cart/items") == gateway.CART_SERVICE_URL


def test_unknown_route_has_no_upstream():
    assert gateway.service_for("/not-a-route") is None


def test_login_and_registration_are_public():
    assert gateway.requires_auth("POST", "/auth/login") is False
    assert gateway.requires_auth("POST", "/auth/register") is False


def test_product_reads_public_but_writes_protected():
    assert gateway.requires_auth("GET", "/products") is False
    assert gateway.requires_auth("GET", "/products/123") is False
    assert gateway.requires_auth("POST", "/products") is True


def test_orders_and_payments_require_token():
    assert gateway.requires_auth("GET", "/orders") is True
    assert gateway.requires_auth("POST", "/payments/create-order") is True


def test_invalid_bearer_token_is_rejected():
    assert gateway.verify_bearer("Bearer definitely-not-a-jwt") is None
