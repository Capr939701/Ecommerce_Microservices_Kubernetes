# Integration Change Log

## Update: incorporate PersonB.zip and standardize runtime databases

1. Added the unique Cart Service capability as a single canonical `services/cart-service` implementation.
2. Routed `/cart` through the API Gateway to Cart Service instead of Order Service.
3. Cart identity now comes from gateway-injected `X-Auth-User-Id` / `X-Auth-User-Role`; client-supplied user IDs are not accepted.
4. Cart checkout sends one order request with all cart lines to the existing Order Service and clears the cart only after a successful order response.
5. Replaced Auth's SQLite implementation with SQLAlchemy/PostgreSQL tables and a PostgreSQL health check.
6. Added a dedicated PostgreSQL database for Cart and updated Compose health dependencies.
7. Removed SQLite runtime defaults from Product and Order. Runtime services require PostgreSQL URLs.
8. Standardized Product/Auth/Cart/Postgres URLs to psycopg2 driver for consistency.
9. Removed per-service Compose files and redundant service README/config copies; root Compose and root README are canonical.
10. Did not copy duplicate Product/Order code or duplicate CSV datasets from PersonB.zip. The stronger integrated Product/Order APIs were retained.
11. Removed bundled local database files and any bytecode/virtual-environment artifacts from the integrated package.

## Verification boundary

Python syntax, Compose YAML parsing, and unit tests can be run in the current environment. A live Compose build/start, PostgreSQL health checks, and Razorpay sandbox checkout must still be verified with Docker available.
