from __future__ import annotations

import os
from typing import Optional

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

load_dotenv()
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
USER_AUTH_URL = os.getenv("USER_AUTH_URL", "http://user-auth-service:8001").rstrip("/")
PRODUCT_SERVICE_URL = os.getenv("PRODUCT_SERVICE_URL", "http://product-service:8002").rstrip("/")
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service:8004").rstrip("/")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8003").rstrip("/")
CART_SERVICE_URL = os.getenv("CART_SERVICE_URL", "http://cart-service:8005").rstrip("/")
TIMEOUT_SECONDS = float(os.getenv("GATEWAY_TIMEOUT_SECONDS", "10"))

app = FastAPI(title="E-commerce API Gateway", version="1.0.0")

# Paths are deliberately explicit so the gateway has a predictable service contract.
ROUTES = (
    ("/auth", USER_AUTH_URL),
    ("/users", USER_AUTH_URL),
    ("/products", PRODUCT_SERVICE_URL),
    ("/inventorys", PRODUCT_SERVICE_URL),
    ("/inventory", PRODUCT_SERVICE_URL),
    ("/cart", CART_SERVICE_URL),
    ("/orders", ORDER_SERVICE_URL),
    ("/payments", PAYMENT_SERVICE_URL),
)
PUBLIC_EXACT = {
    ("POST", "/auth/register"),
    ("POST", "/auth/verify-email"),
    ("POST", "/auth/login"),
    ("POST", "/auth/verify-token"),
}
PUBLIC_PREFIXES = ("/products",)
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


@app.on_event("startup")
def validate_configuration() -> None:
    if not JWT_SECRET or len(JWT_SECRET) < 32:
        raise RuntimeError("JWT_SECRET must be configured and contain at least 32 characters")


@app.get("/health")
def health():
    return {"status": "UP", "service": "api-gateway"}


def service_for(path: str) -> Optional[str]:
    for prefix, base_url in ROUTES:
        if path == prefix or path.startswith(prefix + "/"):
            return base_url
    return None


def requires_auth(method: str, path: str) -> bool:
    if (method.upper(), path.rstrip("/")) in PUBLIC_EXACT:
        return False
    # Product catalogue reads are public; mutations are protected.
    if path == "/products" or path.startswith("/products/"):
        return method.upper() not in {"GET", "HEAD"}
    return path.startswith(("/users", "/inventorys", "/inventory", "/cart", "/orders", "/payments"))


def verify_bearer(value: Optional[str]) -> Optional[dict]:
    if not value or not value.lower().startswith("bearer "):
        return None
    token = value.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    path = "/" + path if path else "/"
    upstream = service_for(path)
    if not upstream:
        return JSONResponse(status_code=404, content={"detail": "No service route configured for this path"})

    auth_payload = verify_bearer(request.headers.get("authorization"))
    if requires_auth(request.method, path) and auth_payload is None:
        return JSONResponse(status_code=401, content={"detail": "A valid Bearer token is required"})

    # Forward the identity as trusted gateway metadata. Downstream services should not
    # accept these headers directly from public traffic; only the gateway is published.
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS and k.lower() not in {"x-auth-user-id", "x-auth-user-role"}}
    if auth_payload:
        headers["x-auth-user-id"] = str(auth_payload.get("sub") or auth_payload.get("id") or "")
        headers["x-auth-user-role"] = str(auth_payload.get("role") or "CUSTOMER")
    query = request.url.query
    url = f"{upstream}{path}" + (f"?{query}" if query else "")
    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            upstream_response = await client.request(
                method=request.method,
                url=url,
                content=body if body else None,
                headers=headers,
            )
    except httpx.TimeoutException:
        return JSONResponse(status_code=504, content={"detail": "Upstream service timed out"})
    except httpx.HTTPError:
        return JSONResponse(status_code=502, content={"detail": "Upstream service is unavailable"})

    response_headers = {
        k: v for k, v in upstream_response.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS and k.lower() not in {"content-encoding"}
    }
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
