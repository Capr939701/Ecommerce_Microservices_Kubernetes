"""Static checks for the single merged Compose topology; does not start containers."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
services = compose["services"]
required = {
    "user-auth-service", "api-gateway", "product-service", "order-service", "payment-service", "cart-service",
    "auth-db", "product-db", "order-db", "payment-db", "cart-db",
}
missing = required - services.keys()
assert not missing, f"Missing services: {sorted(missing)}"
published = [name for name, spec in services.items() if spec.get("ports")]
assert published == ["api-gateway"], f"Only the gateway should publish a host port; got {published}"
env = services["api-gateway"]["environment"]
assert env["PRODUCT_SERVICE_URL"] == "http://product-service:8002"
assert env["ORDER_SERVICE_URL"] == "http://order-service:8004"
assert env["PAYMENT_SERVICE_URL"] == "http://payment-service:8003"
assert env["CART_SERVICE_URL"] == "http://cart-service:8005"
assert services["order-service"]["environment"]["PRODUCT_SERVICE_URL"] == "http://product-service:8002"
assert services["order-service"]["environment"]["PAYMENT_SERVICE_URL"] == "http://payment-service:8003"
for service in ("user-auth-service", "product-service", "order-service", "payment-service", "cart-service"):
    assert services[service]["environment"]["DATABASE_URL"].startswith("postgresql+"), f"{service} must use PostgreSQL"
    assert "healthcheck" in services[service], f"{service} needs a health check"
    assert any(dep.endswith("-db") for dep in services[service].get("depends_on", {})), f"{service} must depend on its database"
assert services["order-service"]["depends_on"]["product-service"]["condition"] == "service_healthy"
assert services["order-service"]["depends_on"]["payment-service"]["condition"] == "service_healthy"
assert services["cart-service"]["depends_on"]["order-service"]["condition"] == "service_healthy"
assert services["api-gateway"]["depends_on"]["cart-service"]["condition"] == "service_healthy"
print("Compose contract checks passed: six application components, five PostgreSQL databases, private internal ports, routing, health checks, and startup dependencies.")
