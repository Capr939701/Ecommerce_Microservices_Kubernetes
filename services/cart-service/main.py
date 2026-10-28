"""Cart Microservice.

Manages user shopping carts, persistent cart line items, pricing aggregations,
stock pre-checks against the Product Service, and atomic checkout transitions
to the Order Service.
Backed by PostgreSQL (with SQLite compatibility for unit tests).
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP
from typing import Generator, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Integer, String, UniqueConstraint, create_engine, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

# ==============================================================================
# Environment Configuration & Service Endpoints
# ==============================================================================

SERVICE_NAME = "cart-service"
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://cart_user:cart_pass@cart-db:5432/cart_db",
)
if not (DATABASE_URL.startswith("postgresql+") or DATABASE_URL.startswith("sqlite")):
    raise RuntimeError("DATABASE_URL must point to PostgreSQL; SQLite is not supported for application runtime")

# Internal service endpoints for inter-service communication
PRODUCT_SERVICE_URL = os.getenv("PRODUCT_SERVICE_URL", "http://product-service:8002").rstrip("/")
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service:8004").rstrip("/")

# Inter-service HTTP call timeouts in seconds
PRODUCT_TIMEOUT = float(os.getenv("PRODUCT_TIMEOUT_SECONDS", "5"))
ORDER_TIMEOUT = float(os.getenv("ORDER_TIMEOUT_SECONDS", "15"))

# Database connection retry parameters
DB_CONNECT_RETRIES = max(1, int(os.getenv("DB_CONNECT_RETRIES", "10")))
DB_CONNECT_RETRY_DELAY_SECONDS = max(0.1, float(os.getenv("DB_CONNECT_RETRY_DELAY_SECONDS", "2")))

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# ==============================================================================
# Database Models (SQLAlchemy ORM)
# ==============================================================================

class Base(DeclarativeBase):
    """Declarative base class for Cart Service models."""
    pass


class CartItem(Base):
    """Cart item line representing a product quantity in a specific user's shopping cart."""

    __tablename__ = "cart_items"
    __table_args__ = (
        UniqueConstraint("user_id", "product_id", name="uq_cart_user_product"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)


# ==============================================================================
# Pydantic Schemas (Request / Response validation)
# ==============================================================================

class CartItemAdd(BaseModel):
    """Payload for adding an item to the user's cart."""
    product_id: str = Field(min_length=1, max_length=36)
    quantity: int = Field(default=1, gt=0, le=10000)


class CartItemUpdate(BaseModel):
    """Payload for updating the quantity of an existing cart item."""
    quantity: int = Field(gt=0, le=10000)


class CheckoutRequest(BaseModel):
    """Payload for checking out the current shopping cart."""
    currency: str = Field(default="INR", min_length=3, max_length=3)


# ==============================================================================
# HTTP Client Management & Database Initialization
# ==============================================================================

http_client: httpx.Client | None = None


def get_http_client() -> httpx.Client:
    """Retrieve or lazily initialize the shared HTTP client for outbound service requests."""
    global http_client
    if http_client is None or http_client.is_closed:
        http_client = httpx.Client(timeout=ORDER_TIMEOUT)
    return http_client


def init_db() -> None:
    """Initialize database tables with connection retry logic."""
    if str(DATABASE_URL).startswith("sqlite") or str(getattr(engine, "url", "")).startswith("sqlite"):
        Base.metadata.create_all(bind=engine)
        return

    last_error: Exception | None = None
    for attempt in range(1, DB_CONNECT_RETRIES + 1):
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            Base.metadata.create_all(bind=engine)
            return
        except Exception as exc:
            last_error = exc
            if attempt < DB_CONNECT_RETRIES:
                time.sleep(DB_CONNECT_RETRY_DELAY_SECONDS)

    raise RuntimeError(
        f"Cart Service could not connect to its configured database after "
        f"{DB_CONNECT_RETRIES} attempts. Check DATABASE_URL and database readiness."
    ) from last_error


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Lifespan context manager: initializes DB and manages HTTP client session lifecycle."""
    global http_client
    init_db()
    http_client = httpx.Client(timeout=ORDER_TIMEOUT)
    yield
    if http_client and not http_client.is_closed:
        http_client.close()


app = FastAPI(
    title="E-Commerce Cart Service",
    description="Shopping cart and checkout orchestrator",
    version="1.0.0",
    lifespan=lifespan,
)


# ==============================================================================
# Dependencies & Helper Functions
# ==============================================================================

def get_db() -> Generator[Session, None, None]:
    """Dependency that yields a database session and ensures proper closure."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def identity(
    x_auth_user_id: Optional[str] = Header(None),
    x_auth_user_role: Optional[str] = Header(None),
) -> tuple[str, str]:
    """Extract authenticated identity headers injected by the API gateway."""
    if not x_auth_user_id:
        raise HTTPException(status_code=401, detail="Authenticated user identity is required")
    return x_auth_user_id, (x_auth_user_role or "CUSTOMER").upper()


def fetch_product(product_id: str, user_id: str, role: str) -> dict:
    """Fetch live product details and inventory availability from the Product Service."""
    client = get_http_client()
    try:
        response = client.get(
            f"{PRODUCT_SERVICE_URL}/products/{product_id}",
            timeout=PRODUCT_TIMEOUT,
            headers={"X-Auth-User-Id": user_id, "X-Auth-User-Role": role},
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Product Service timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Product Service is unavailable") from exc

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Product {product_id} not found")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Product Service could not retrieve product")

    return response.json()


def find_item(db: Session, user_id: str, product_id: str) -> Optional[CartItem]:
    """Look up an existing cart item for a given user and product ID."""
    return db.scalar(
        select(CartItem).where(CartItem.user_id == user_id, CartItem.product_id == product_id)
    )


# ==============================================================================
# Endpoints & Route Handlers
# ==============================================================================

@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    """Service health and database probe endpoint."""
    try:
        db.execute(text("SELECT 1"))
        return {"status": "UP", "service": SERVICE_NAME, "database": "UP"}
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"status": "DOWN", "service": SERVICE_NAME, "database": "DOWN"},
        ) from exc


@app.post("/cart/items", status_code=201)
def add_to_cart(
    item: CartItemAdd,
    db: Session = Depends(get_db),
    who: tuple[str, str] = Depends(identity),
) -> dict:
    """Add a product quantity to the user's shopping cart after validating stock."""
    user_id, role = who

    # Verify that the product is active and published
    product = fetch_product(item.product_id, user_id, role)
    if product.get("status") != "PUBLISHED":
        raise HTTPException(status_code=409, detail="Product is not available for purchase")

    # Calculate cumulative quantity if product already exists in cart
    existing = find_item(db, user_id, item.product_id)
    new_quantity = item.quantity + (existing.quantity if existing else 0)

    # Validate stock limits
    if int(product.get("stock", 0)) < new_quantity:
        raise HTTPException(
            status_code=409,
            detail=f"Not enough stock. Available: {product.get('stock', 0)}, requested: {new_quantity}",
        )

    if existing:
        existing.quantity = new_quantity
    else:
        db.add(CartItem(user_id=user_id, product_id=item.product_id, quantity=item.quantity))

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Cart item conflicted with another update; retry the request",
        ) from exc

    return {
        "message": f"{product['name']} added to cart",
        "product_id": item.product_id,
        "quantity_in_cart": new_quantity,
    }


@app.get("/cart")
def view_cart(
    db: Session = Depends(get_db),
    who: tuple[str, str] = Depends(identity),
) -> dict:
    """View the calling user's cart with live prices, product names, and subtotal."""
    user_id, role = who
    rows = db.scalars(select(CartItem).where(CartItem.user_id == user_id).order_by(CartItem.id)).all()

    items = []
    total = Decimal("0.00")

    for row in rows:
        try:
            product = fetch_product(row.product_id, user_id, role)
        except HTTPException as exc:
            if exc.status_code in {503, 504, 502}:
                raise
            items.append({
                "product_id": row.product_id,
                "quantity": row.quantity,
                "available": False,
                "note": "Product no longer exists",
            })
            continue

        price = Decimal(str(product["price"]))
        subtotal = (price * row.quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total += subtotal

        items.append({
            "product_id": row.product_id,
            "name": product["name"],
            "sku": product.get("sku"),
            "price": str(price),
            "quantity": row.quantity,
            "subtotal": str(subtotal),
            "in_stock": int(product.get("stock", 0)) >= row.quantity,
            "available": True,
        })

    return {
        "items": items,
        "total": str(total.quantize(Decimal("0.01"))),
        "count": len(items),
    }


@app.put("/cart/items/{product_id}")
def update_item(
    product_id: str,
    updated: CartItemUpdate,
    db: Session = Depends(get_db),
    who: tuple[str, str] = Depends(identity),
) -> dict:
    """Update the quantity of a specific product line in the user's cart."""
    user_id, role = who
    row = find_item(db, user_id, product_id)
    if not row:
        raise HTTPException(status_code=404, detail="Item not in cart")

    # Verify inventory availability for updated quantity
    product = fetch_product(product_id, user_id, role)
    if int(product.get("stock", 0)) < updated.quantity:
        raise HTTPException(
            status_code=409,
            detail=f"Not enough stock. Available: {product.get('stock', 0)}, requested: {updated.quantity}",
        )

    row.quantity = updated.quantity
    db.commit()

    return {
        "message": "Cart quantity updated",
        "product_id": product_id,
        "quantity": updated.quantity,
    }


@app.delete("/cart/items/{product_id}", status_code=200)
def remove_item(
    product_id: str,
    db: Session = Depends(get_db),
    who: tuple[str, str] = Depends(identity),
) -> dict:
    """Delete a single product line item from the user's cart."""
    user_id, _role = who
    row = find_item(db, user_id, product_id)
    if not row:
        raise HTTPException(status_code=404, detail="Item not in cart")

    db.delete(row)
    db.commit()

    return {"message": "Item removed from cart"}


@app.delete("/cart", status_code=200)
def clear_cart(
    db: Session = Depends(get_db),
    who: tuple[str, str] = Depends(identity),
) -> dict:
    """Remove all items from the current user's shopping cart."""
    user_id, _role = who
    db.query(CartItem).filter(CartItem.user_id == user_id).delete(synchronize_session=False)
    db.commit()

    return {"message": "Cart cleared"}


@app.post("/cart/checkout", status_code=201)
def checkout(
    details: CheckoutRequest,
    db: Session = Depends(get_db),
    who: tuple[str, str] = Depends(identity),
) -> dict:
    """Submit the contents of the cart to create an Order and clear the cart upon success."""
    user_id, role = who
    rows = db.scalars(select(CartItem).where(CartItem.user_id == user_id).order_by(CartItem.id)).all()

    if not rows:
        raise HTTPException(status_code=400, detail="Cart is empty")

    payload = {
        "items": [{"product_id": row.product_id, "quantity": row.quantity} for row in rows],
        "currency": details.currency.upper(),
    }

    client = get_http_client()
    try:
        response = client.post(
            f"{ORDER_SERVICE_URL}/orders",
            json=payload,
            timeout=ORDER_TIMEOUT,
            headers={"X-Auth-User-Id": user_id, "X-Auth-User-Role": role},
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail="Order Service timed out; check order history before retrying",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Order Service is unavailable") from exc

    if response.status_code not in {200, 201}:
        try:
            detail = response.json().get("detail", "Order creation failed")
        except Exception:
            detail = "Order creation failed"
        raise HTTPException(
            status_code=response.status_code if response.status_code in {400, 404, 409, 422} else 502,
            detail=detail,
        )

    order = response.json()

    # Clear cart only after Order Service confirms durable order creation
    db.query(CartItem).filter(CartItem.user_id == user_id).delete(synchronize_session=False)
    db.commit()

    return {
        "status": "success",
        "order": order,
        "message": "Checkout submitted; continue payment using the returned order details",
    }
