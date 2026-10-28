"""API Gateway Microservice for E-Commerce Platform.

Acts as the single public entry point for all incoming HTTP requests.
Handles:
  1. Route dispatching and reverse-proxying to internal microservices.
  2. Centralized JWT authentication and signature verification.
  3. Header sanitization (stripping hop-by-hop and spoofed identity headers).
  4. Identity propagation (injecting verified 'X-Auth-User-Id' and 'X-Auth-User-Role').
  5. Inter-service clock drift tolerance (JWT leeway=30s).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


# =====================================================================
# Configuration and Environment Variables
# =====================================================================

load_dotenv()

# JWT Secret and Algorithm for verifying user identity tokens
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"

# Internal microservice endpoint URLs (resolved via Docker network DNS)
USER_AUTH_URL = os.getenv("USER_AUTH_URL", "http://user-auth-service:8001").rstrip("/")
PRODUCT_SERVICE_URL = os.getenv("PRODUCT_SERVICE_URL", "http://product-service:8002").rstrip("/")
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service:8004").rstrip("/")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8003").rstrip("/")
CART_SERVICE_URL = os.getenv("CART_SERVICE_URL", "http://cart-service:8005").rstrip("/")

# Downstream HTTP client timeout in seconds
TIMEOUT_SECONDS = float(os.getenv("GATEWAY_TIMEOUT_SECONDS", "10"))


def validate_configuration() -> None:
    """Validate that required environment variables and secrets meet security thresholds."""
    if not JWT_SECRET or len(JWT_SECRET) < 32:
        raise RuntimeError("JWT_SECRET must be configured and contain at least 32 characters")


# =====================================================================
# Application Lifecycle
# =====================================================================

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Application lifespan context manager for startup validation and cleanup."""
    validate_configuration()
    yield


app = FastAPI(
    title="E-commerce API Gateway",
    version="1.0.0",
    description="Central reverse proxy and authentication gateway for e-commerce microservices",
    lifespan=lifespan,
)


# =====================================================================
# Routing Table and Authorization Rules
# =====================================================================

# Explicit routing table mapping URL path prefixes to downstream service base URLs.
ROUTES = (
    ("/auth", USER_AUTH_URL),
    ("/users", USER_AUTH_URL),
    ("/products", PRODUCT_SERVICE_URL),
    ("/inventory", PRODUCT_SERVICE_URL),
    ("/cart", CART_SERVICE_URL),
    ("/orders", ORDER_SERVICE_URL),
    ("/payments", PAYMENT_SERVICE_URL),
)

# Public endpoints that do not require an Authorization Bearer token.
PUBLIC_EXACT = {
    ("POST", "/auth/register"),
    ("POST", "/auth/verify-email"),
    ("POST", "/auth/login"),
    ("POST", "/auth/verify-token"),
}

# Hop-by-hop headers defined by RFC 2616 that must not be forwarded by a proxy.
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


# =====================================================================
# Utility and Authentication Helpers
# =====================================================================

def service_for(path: str) -> Optional[str]:
    """Resolve the upstream service URL matching a given request path prefix."""
    for prefix, base_url in ROUTES:
        if path == prefix or path.startswith(prefix + "/"):
            return base_url
    return None


def requires_auth(method: str, path: str) -> bool:
    """Determine whether an incoming request method and path require JWT authentication.
    
    - Authentication registration/login/verification endpoints are public.
    - Product catalog read requests (GET/HEAD) are public.
    - Product mutations (POST/PUT/PATCH/DELETE) and all cart, order, payment,
      user, and inventory operations require a valid authenticated identity.
    """
    if (method.upper(), path.rstrip("/")) in PUBLIC_EXACT:
        return False

    # Product catalog reads are public; administrative mutations are protected
    if path == "/products" or path.startswith("/products/"):
        return method.upper() not in {"GET", "HEAD"}

    return path.startswith(("/users", "/inventory", "/cart", "/orders", "/payments"))


def verify_bearer(value: Optional[str]) -> Optional[dict]:
    """Validate and decode a Bearer JWT token from the Authorization header.
    
    Uses leeway=30 seconds to tolerate clock drift between container hosts.
    Returns the decoded token payload if valid, or None if missing/invalid/expired.
    """
    if not value or not value.lower().startswith("bearer "):
        return None

    token = value.split(" ", 1)[1].strip()
    if not token:
        return None

    try:
        # leeway=30 prevents spurious 401s caused by microsecond clock skew across containers
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM], leeway=30)
    except jwt.PyJWTError:
        return None


# =====================================================================
# Route Handlers
# =====================================================================

@app.get("/health")
def health() -> dict[str, str]:
    """Gateway health check endpoint reporting service operational status."""
    return {"status": "UP", "service": "api-gateway"}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request) -> Response:
    """Universal reverse proxy route.
    
    1. Matches the requested path against configured downstream microservices.
    2. Enforces authentication rules and decodes Bearer JWT tokens.
    3. Strips untrusted identity headers (x-auth-user-id, x-auth-user-role) from client traffic.
    4. Injects verified identity headers if authentication succeeds.
    5. Proxies the request payload to the upstream microservice and streams back the response.
    """
    path = "/" + path if path else "/"
    upstream = service_for(path)
    if not upstream:
        return JSONResponse(
            status_code=404,
            content={"detail": "No service route configured for this path"},
        )

    auth_payload = verify_bearer(request.headers.get("authorization"))
    if requires_auth(request.method, path) and auth_payload is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "A valid Bearer token is required"},
        )

    # Sanitize client headers: strip hop-by-hop headers and prevent header spoofing
    # of 'x-auth-user-id' and 'x-auth-user-role'. Only the gateway injects trusted identity.
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
        and k.lower() not in {"x-auth-user-id", "x-auth-user-role"}
    }

    # Inject verified user identity metadata for downstream microservices
    if auth_payload:
        headers["x-auth-user-id"] = str(auth_payload.get("sub") or auth_payload.get("id") or "")
        headers["x-auth-user-role"] = str(auth_payload.get("role") or "CUSTOMER")

    query = request.url.query
    url = f"{upstream}{path}" + (f"?{query}" if query else "")
    body = await request.body()

    # Forward the HTTP request to the designated downstream microservice
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            upstream_response = await client.request(
                method=request.method,
                url=url,
                content=body if body else None,
                headers=headers,
            )
    except httpx.TimeoutException:
        return JSONResponse(
            status_code=504,
            content={"detail": "Upstream service timed out"},
        )
    except httpx.HTTPError:
        return JSONResponse(
            status_code=502,
            content={"detail": "Upstream service is unavailable"},
        )

    # Clean upstream response headers before returning to client
    response_headers = {
        k: v for k, v in upstream_response.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
        and k.lower() not in {"content-encoding"}
    }

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
