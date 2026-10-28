"""API Gateway Microservice.

Acts as the single front-door reverse proxy for the entire e-commerce platform.
Responsibilities:
- Routes incoming client HTTP requests to appropriate downstream microservices.
- Enforces Bearer JWT authentication for protected endpoints.
- Injects authenticated user identity (`X-Auth-User-Id`, `X-Auth-User-Role`) as trusted
  headers for internal microservices.
- Filters out hop-by-hop HTTP headers and spoofed identity headers from untrusted clients.
- Normalizes upstream network timeouts and connection errors into standard HTTP responses.
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

# ==============================================================================
# Environment Configuration & Constants
# ==============================================================================

try:
    load_dotenv()
except (PermissionError, OSError):
    pass

# Secret key and algorithm used for cryptographic verification of JWT tokens.
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"

# Internal service URLs (resolved via Docker Compose internal DNS or Kubernetes Service names).
USER_AUTH_URL = os.getenv("USER_AUTH_URL", "http://user-auth-service:8001").rstrip("/")
PRODUCT_SERVICE_URL = os.getenv("PRODUCT_SERVICE_URL", "http://product-service:8002").rstrip("/")
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service:8004").rstrip("/")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8003").rstrip("/")
CART_SERVICE_URL = os.getenv("CART_SERVICE_URL", "http://cart-service:8005").rstrip("/")

# Gateway proxy request timeout in seconds.
TIMEOUT_SECONDS = float(os.getenv("GATEWAY_TIMEOUT_SECONDS", "10"))

# Service route registry: Maps URL path prefixes to upstream microservice base URLs.
# Paths are evaluated in order; explicit prefixes ensure deterministic routing contracts.
ROUTES = (
    ("/auth", USER_AUTH_URL),
    ("/users", USER_AUTH_URL),
    ("/products", PRODUCT_SERVICE_URL),
    ("/inventory", PRODUCT_SERVICE_URL),
    ("/cart", CART_SERVICE_URL),
    ("/orders", ORDER_SERVICE_URL),
    ("/payments", PAYMENT_SERVICE_URL),
)

# Set of exact (HTTP_METHOD, PATH) combinations that do NOT require authentication.
PUBLIC_EXACT = {
    ("POST", "/auth/register"),
    ("POST", "/auth/verify-email"),
    ("POST", "/auth/login"),
    ("POST", "/auth/verify-token"),
}

# RFC 2616 / RFC 7230 hop-by-hop headers that must not be forwarded by proxies.
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


# ==============================================================================
# Startup Validation & Application Lifespan
# ==============================================================================

def validate_configuration() -> None:
    """Validate that critical environment variables are properly set before serving traffic."""
    if not JWT_SECRET or len(JWT_SECRET) < 32:
        raise RuntimeError("JWT_SECRET must be configured and contain at least 32 characters")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """FastAPI lifespan context manager: handles startup configuration checks and graceful shutdown."""
    validate_configuration()
    yield


app = FastAPI(
    title="E-commerce API Gateway",
    description="Unified API Gateway and Reverse Proxy for E-commerce Microservices",
    version="1.0.0",
    lifespan=lifespan,
)


# ==============================================================================
# Routing & Authentication Helpers
# ==============================================================================

def service_for(path: str) -> Optional[str]:
    """Map a request URL path to its designated upstream service base URL.

    Args:
        path: The normalized request path (e.g., '/products/123').

    Returns:
        The matching upstream base URL (e.g., 'http://product-service:8002'),
        or None if no route rule matches.
    """
    for prefix, base_url in ROUTES:
        if path == prefix or path.startswith(prefix + "/"):
            return base_url
    return None


def requires_auth(method: str, path: str) -> bool:
    """Determine whether an incoming HTTP request requires Bearer token authentication.

    Rules:
    - Endpoints listed in PUBLIC_EXACT (registration, login, verification) are public.
    - Product catalogue browsing (GET/HEAD on /products) is public.
    - All inventory, user profile, cart, order, and payment operations require auth.

    Args:
        method: HTTP method (e.g. 'GET', 'POST').
        path: Normalized request URL path.

    Returns:
        True if the request must present a valid Bearer token, False otherwise.
    """
    if (method.upper(), path.rstrip("/")) in PUBLIC_EXACT:
        return False

    # Product catalogue reads are public; modifications (POST, PUT, DELETE) require admin auth.
    if path == "/products" or path.startswith("/products/"):
        return method.upper() not in {"GET", "HEAD"}

    return path.startswith(("/users", "/inventory", "/cart", "/orders", "/payments"))


def verify_bearer(value: Optional[str]) -> Optional[dict]:
    """Validate and decode a Bearer JWT token from an Authorization header.

    Args:
        value: The raw Authorization header string (e.g. 'Bearer eyJhbGci...').

    Returns:
        The decoded JWT claims dictionary if valid, or None if missing/invalid/expired.
    """
    if not value or not value.lower().startswith("bearer "):
        return None

    token = value.split(" ", 1)[1].strip()
    if not token:
        return None

    try:
        # Include 30 seconds leeway to tolerate minor clock skew across distributed containers.
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM], leeway=30)
    except jwt.PyJWTError:
        return None


# ==============================================================================
# Endpoints & Reverse Proxy Handler
# ==============================================================================

@app.get("/health")
def health() -> dict:
    """Gateway health check endpoint to verify uptime."""
    return {"status": "UP", "service": "api-gateway"}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    """Catch-all reverse proxy route.

    Coordinates authentication, header sanitation, upstream forwarding,
    and downstream response streaming.

    Workflow:
    1. Resolves destination upstream microservice.
    2. Validates Bearer token if endpoint requires authentication.
    3. Strips hop-by-hop and client-injected identity headers.
    4. Injects verified `X-Auth-User-Id` and `X-Auth-User-Role` headers.
    5. Dispatches asynchronous HTTP request to downstream service.
    6. Returns upstream response or friendly gateway error (502 / 504).
    """
    path = "/" + path if path else "/"
    upstream = service_for(path)
    if not upstream:
        return JSONResponse(
            status_code=404,
            content={"detail": "No service route configured for this path"},
        )

    # Validate authentication token if required.
    auth_payload = verify_bearer(request.headers.get("authorization"))
    if requires_auth(request.method, path) and auth_payload is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "A valid Bearer token is required"},
        )

    # Sanitize request headers:
    # 1. Strip hop-by-hop headers that should not be forwarded across proxy hops.
    # 2. Strip client-supplied X-Auth-* headers to prevent header injection / spoofing.
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS and k.lower() not in {"x-auth-user-id", "x-auth-user-role"}
    }

    # Inject verified user identity into downstream headers.
    if auth_payload:
        headers["x-auth-user-id"] = str(auth_payload.get("sub") or auth_payload.get("id") or "")
        headers["x-auth-user-role"] = str(auth_payload.get("role") or "CUSTOMER")

    # Construct target upstream URL preserving query parameters.
    query = request.url.query
    url = f"{upstream}{path}" + (f"?{query}" if query else "")
    body = await request.body()

    # Dispatch proxy call upstream.
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

    # Strip hop-by-hop and content-encoding headers from upstream response.
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
