#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# One-Click End-to-End Test Runner for E-Commerce Microservices
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

export GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:8000}"
export ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-AdminPassword123!}"

echo "======================================================================"
echo "          E-COMMERCE MICROSERVICES ALL-IN-ONE TEST RUNNER             "
echo "======================================================================"
echo "Target Gateway: ${GATEWAY_URL}"
echo "Working Dir:    ${ROOT_DIR}"
echo ""

# 1. Check if Docker daemon is running
if ! docker info >/dev/null 2>&1; then
  echo "Error: Docker daemon is not accessible. Please start Docker Desktop." >&2
  exit 1
fi

# 2. Check if containers are already running; start them if not
RUNNING_CONTAINERS=$(docker compose ps --status running -q 2>/dev/null | wc -l || echo "0")
if [ "${RUNNING_CONTAINERS}" -lt 11 ]; then
  echo "Starting / updating Docker Compose containers..."
  docker compose up -d
else
  echo "Containers already running (Count: ${RUNNING_CONTAINERS})."
fi

# 3. Wait for API Gateway to be healthy and responsive
echo -n "Waiting for API Gateway at ${GATEWAY_URL}/health ..."
MAX_RETRIES=30
COUNT=0
until curl -s -f "${GATEWAY_URL}/health" >/dev/null 2>&1; do
  COUNT=$((COUNT + 1))
  if [ "${COUNT}" -ge "${MAX_RETRIES}" ]; then
    echo " FAILED."
    echo "Error: API Gateway did not become healthy in time." >&2
    docker compose ps
    exit 1
  fi
  echo -n "."
  sleep 2
done
echo " READY!"
echo ""

# 4. Display Docker Compose container health
echo "--- Container Status ---"
docker compose ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo ""

# 5. Detect Python interpreter (python3 or python)
PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "Error: Python interpreter (python or python3) not found in PATH." >&2
  exit 1
fi

# 6. Run the full End-to-End test suite
echo "Running automated end-to-end integration test suite..."
"${PYTHON_BIN}" scripts/test_microservices_e2e.py
TEST_EXIT_CODE=$?

if [ ${TEST_EXIT_CODE} -eq 0 ]; then
  echo ""
  echo "======================================================================"
  echo "           ALL MICROSERVICES FEATURES VERIFIED SUCCESSFULLY!         "
  echo "======================================================================"
else
  echo ""
  echo "======================================================================"
  echo "            SOME TESTS FAILED (EXIT CODE: ${TEST_EXIT_CODE})          "
  echo "======================================================================"
fi

exit ${TEST_EXIT_CODE}
