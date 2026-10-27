#!/usr/bin/env bash
set -e

# Run the Phase 3 automated test suite
# Administrator credentials may be provided via ADMIN_PASSWORD environment variable
# or prompted interactively by python if omitted.
GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:8000}" \
ADMIN_EMAIL="${ADMIN_EMAIL:-testcustomer@example.com}" \
ADMIN_PASSWORD="${ADMIN_PASSWORD}" \
python3 scripts/test_microservices_e2e.py "$@"
