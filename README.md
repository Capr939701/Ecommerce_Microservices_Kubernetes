# Integrated E-Commerce Microservices

This is the **single canonical integrated project**. Use this root folder and its one root `docker-compose.yml`; do not combine individual ZIPs or copy duplicate Product/Order services into it.

## What was incorporated from the newly received `PersonB.zip`

- Reviewed its Product, Order, and Cart implementations.
- Kept the existing integrated Product and Order services because they already support stronger contracts needed by inventory reservations, ownership checks, and Razorpay payment coordination. The simpler PersonB Product/Order files would duplicate these services and break the current API contract if copied over.
- Incorporated the unique Cart Service capability as `services/cart-service`, adapted to the integrated Product and Order APIs and to authenticated identity headers from the Gateway.
- Did **not** copy the two duplicate `Online Sales Data.csv` files or the bundled `venv`, `__pycache__`, `.pyc`, local database files, or the old per-service Compose files.

## Services and ports

| Component | Internal port | Database |
|---|---:|---|
| API Gateway | 8000 | none |
| User/Auth | 8001 | PostgreSQL `auth_db` |
| Product + Inventory | 8002 | PostgreSQL `product_db` |
| Payment | 8003 | PostgreSQL `payment_db` |
| Order | 8004 | PostgreSQL `order_db` |
| Cart | 8005 | PostgreSQL `cart_db` |

All five business services use separate PostgreSQL databases. No application runtime falls back to SQLite. The only SQLite use remaining is in isolated in-memory unit-test fixtures, where it is a temporary test backend and is not used by Docker/application runtime. No SQLite database files are shipped.

## Run locally

1. Install Docker Desktop / Docker Compose v2.
2. Copy `.env.example` to `.env`.
3. Set a random `JWT_SECRET` of at least 32 characters, unique database passwords, and Razorpay **test** credentials.
4. Run `docker compose up --build -d`.
5. Inspect `docker compose ps` and `docker compose logs --tail=100`.
6. Open `http://localhost:8000/docs`.

Only the API Gateway is published to the host. Internal service ports are available only on the Compose network.

## Main workflow

Register → verify email → login → browse products → add products to cart → checkout → inventory reservation → Razorpay payment → payment verification → order history.

The Cart Service routes are `POST /cart/items`, `GET /cart`, `PUT /cart/items/{product_id}`, `DELETE /cart/items/{product_id}`, `DELETE /cart`, and `POST /cart/checkout`. All require a valid Gateway JWT. Cart ownership is derived from the Gateway-injected user identity; clients cannot choose another user's ID in the URL.

## Important local-demo notes

- `AUTH_EXPOSE_VERIFICATION_CODE=true` exposes the verification code in registration responses for local demonstration only; no real email provider is configured.
- New users have the `CUSTOMER` role. Product changes require `ADMIN` or `PRODUCT_MANAGER`; use a controlled local admin bootstrap process, never a public self-promotion endpoint.
- Payment requires valid Razorpay test credentials and checkout. A passing mocked unit test is not proof of a real sandbox transaction.
- Existing SQLite user data is **not automatically migrated** into PostgreSQL. This package starts with a fresh PostgreSQL Auth database. If you need to retain existing SQLite users, migrate them explicitly before switching.
- Do not run `docker compose down -v` unless you intend to delete all named database volumes.

## Tests

Unit tests for individual services use isolated test fixtures/mocked downstream services. Some tests use in-memory SQLite solely as a temporary testing backend; application runtime is PostgreSQL-only. Live Docker Compose, PostgreSQL, and Razorpay sandbox verification must be run on a machine with Docker and valid test credentials.

## Kubernetes / Minikube

See [`k8s/README.md`](k8s/README.md) for build, deploy, access, and troubleshooting steps. Apply [`k8s/ecommerce-all.yaml`](k8s/ecommerce-all.yaml) after creating the `ecommerce-secrets` Secret and building the six application images into Minikube.
