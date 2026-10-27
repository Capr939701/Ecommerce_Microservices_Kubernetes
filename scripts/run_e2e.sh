#!/usr/bin/env bash
set -e

if [ -f "/tmp/.admin_pwd" ]; then
    ADMIN_PASSWORD=$(cat /tmp/.admin_pwd)
fi

GATEWAY_URL="http://127.0.0.1:8000" ADMIN_PASSWORD="$ADMIN_PASSWORD" python3 scripts/test_microservices_e2e.py
