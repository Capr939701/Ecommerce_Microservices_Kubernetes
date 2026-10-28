import os
import sys
import json
import subprocess
import urllib.request
import urllib.error

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "AdminPassword123!")

def post_json(path, data, token=None):
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

def get_json(path, token=None):
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

def bootstrap():
    print(f"Bootstrapping administrator ({ADMIN_EMAIL}) and LAPTOP-001 if needed...")

    # 1. Register administrator if not exists
    status, body = post_json("/auth/register", {
        "name": "Administrator",
        "email": ADMIN_EMAIL,
        "password": ADMIN_PASSWORD,
    })
    if status == 201:
        code = body.get("verificationCode")
        print(f"Registered admin, verification code: {code}")
        if code:
            v_status, v_body = post_json("/auth/verify-email", {
                "email": ADMIN_EMAIL,
                "code": code,
            })
            print(f"Verified admin email: status {v_status}")
    elif status == 409 or (status == 400 and "already" in str(body).lower()):
        print("Admin account already registered.")
    else:
        print(f"Registration response: HTTP {status} {body}")

    # 2. Promote to ADMIN in auth-db
    try:
        cmd = ["docker", "compose", "exec", "-T", "auth-db", "psql", "-U", "auth_user", "-d", "auth_db", "-c", f"UPDATE users SET role='ADMIN', verified=true, status='ACTIVE' WHERE email='{ADMIN_EMAIL}';"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        print(f"Promoted admin in database: {res.stdout.strip()}")
    except Exception as e:
        print(f"Notice: Could not run docker compose psql directly: {e}")

    # 3. Log in as admin
    status, login_body = post_json("/auth/login", {
        "email": ADMIN_EMAIL,
        "password": ADMIN_PASSWORD,
    })
    if status != 200 or not login_body.get("token"):
        print(f"Failed to log in as administrator: HTTP {status} {login_body}")
        return False
    admin_token = login_body["token"]
    print("Obtained administrator token successfully.")

    # 4. Check if LAPTOP-001 exists
    status, catalog = get_json("/products")
    existing_items = catalog.get("data", []) if isinstance(catalog, dict) else []
    laptop = next((p for p in existing_items if p.get("sku") == "LAPTOP-001"), None)
    if not laptop:
        status, prod_body = post_json("/products", {
            "sku": "LAPTOP-001",
            "name": "Pro Developer Laptop 16-inch",
            "description": "High performance laptop with 32GB RAM",
            "price": "1499.99",
            "initial_stock": 10,
            "status": "PUBLISHED",
        }, token=admin_token)
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
    bootstrap()
