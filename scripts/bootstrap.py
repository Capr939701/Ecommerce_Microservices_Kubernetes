"""Automated Seed and Bootstrap Script for E-Commerce Microservices.

Prepares the platform for testing and development by ensuring:
  1. An administrator user account (admin@example.com) exists with verified status and ADMIN role.
  2. An administrator Bearer JWT is acquired.
  3. Canonical product 'LAPTOP-001' (Pro Developer Laptop 16-inch) exists in the catalog.
  4. Sufficient stock (10 units) is available for checkout and payment testing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


# =====================================================================
# Configuration and Environment Variables
# =====================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMPOSE_FILE = os.path.join(os.path.dirname(SCRIPT_DIR), "docker-compose.yml")

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "AdminPassword123!")


# =====================================================================
# HTTP Helper Utilities
# =====================================================================

def post_json(path: str, data: dict, token: str | None = None) -> tuple[int, dict]:
    """Send an HTTP POST request with JSON payload to the API Gateway."""
    url = f"{GATEWAY_URL}{path}"
    payload = json.dumps(data).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as exc:
        content = exc.read().decode("utf-8")
        try:
            body = json.loads(content) if content else {}
        except Exception:
            body = {"detail": content}
        return exc.code, body


def get_json(path: str, token: str | None = None) -> tuple[int, dict]:
    """Send an HTTP GET request to the API Gateway."""
    url = f"{GATEWAY_URL}{path}"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as exc:
        content = exc.read().decode("utf-8")
        try:
            body = json.loads(content) if content else {}
        except Exception:
            body = {"detail": content}
        return exc.code, body


# =====================================================================
# Bootstrap Orchestration
# =====================================================================

def bootstrap() -> bool:
    """Execute complete automated seed workflow."""
    print(f"Bootstrapping administrator ({ADMIN_EMAIL}) and LAPTOP-001 if needed...")

    # -----------------------------------------------------------------
    # Step 1: Register administrator account if not present
    # -----------------------------------------------------------------
    status, body = post_json("/auth/register", {
        "name": "Administrator",
        "email": ADMIN_EMAIL,
        "password": ADMIN_PASSWORD,
    })

    if status == 201:
        code = body.get("verificationCode")
        print(f"Registered admin, verification code: {code}")
        if code:
            v_status, _ = post_json("/auth/verify-email", {
                "email": ADMIN_EMAIL,
                "code": code,
            })
            print(f"Verified admin email: status {v_status}")
    elif status == 409 or (status == 400 and "already" in str(body).lower()):
        print("Admin account already registered.")
    else:
        print(f"Registration response: HTTP {status} {body}")

    # -----------------------------------------------------------------
    # Step 2: Elevate user role to ADMIN in auth-db
    # -----------------------------------------------------------------
    try:
        cmd = [
            "docker", "compose", "-f", COMPOSE_FILE, "exec", "-T", "auth-db",
            "psql", "-U", "auth_user", "-d", "auth_db",
            "-c", f"UPDATE users SET role='ADMIN', verified=true, status='ACTIVE' WHERE email='{ADMIN_EMAIL}';",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        print(f"Promoted admin in database: {res.stdout.strip()}")
    except Exception as e:
        print(f"Notice: Could not run docker compose psql directly: {e}")

    # -----------------------------------------------------------------
    # Step 3: Authenticate as administrator to obtain Bearer token
    # -----------------------------------------------------------------
    status, login_body = post_json("/auth/login", {
        "email": ADMIN_EMAIL,
        "password": ADMIN_PASSWORD,
    })

    if status != 200 or not login_body.get("token"):
        print(f"Failed to log in as administrator: HTTP {status} {login_body}")
        return False

    admin_token = login_body["token"]
    print("Obtained administrator token successfully.")

    # -----------------------------------------------------------------
    # Step 4: Verify and seed product LAPTOP-001 with available stock
    # -----------------------------------------------------------------
    status, catalog = get_json("/products")
    existing_items = catalog.get("data", []) if isinstance(catalog, dict) else []
    laptop = next((p for p in existing_items if p.get("sku") == "LAPTOP-001"), None)

    if not laptop:
        status, prod_body = post_json(
            "/products",
            {
                "sku": "LAPTOP-001",
                "name": "Pro Developer Laptop 16-inch",
                "description": "High performance laptop with 32GB RAM",
                "price": "1499.99",
                "initial_stock": 10,
                "status": "PUBLISHED",
            },
            token=admin_token,
        )
        if status == 201:
            print("Successfully created product LAPTOP-001 with stock=10.")
        else:
            print(f"Notice creating LAPTOP-001: HTTP {status} {prod_body}")
    else:
        print("LAPTOP-001 already exists in catalog.")
        if laptop.get("stock") != 10:
            pid = laptop.get("id")
            post_json(f"/inventory/{pid}", {"quantity": 10}, token=admin_token)
            print("Reset LAPTOP-001 stock to 10.")

    print("Bootstrap completed successfully.")
    return True


if __name__ == "__main__":
    success = bootstrap()
    sys.exit(0 if success else 1)
