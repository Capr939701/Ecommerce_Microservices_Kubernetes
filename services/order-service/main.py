"""Order Microservice.

Coordinates order creation, item calculations, stock reservation via the Product Service,
payment initialization and signature verification via the Payment Service,
order listing, and cancellation with inventory rollback.
Backed by PostgreSQL (with SQLite compatibility for unit testing).
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Generator, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, create_engine, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from typing_extensions import Annotated

# ==============================================================================
# Environment Configuration & Service Endpoints
# ==============================================================================

SERVICE_NAME = "order-service"
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://order_user:order_pass@order-db:5432/order_db",
)
if not (DATABASE_URL.startswith("postgresql+") or DATABASE_URL.startswith("sqlite")):
    raise RuntimeError("DATABASE_URL must point to PostgreSQL; SQLite is not supported for application runtime")

PRODUCT_SERVICE_URL = os.getenv("PRODUCT_SERVICE_URL", "http://product-service:8002").rstrip("/")
PAYMENT_SERVICE_URL = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8003").rstrip("/")

PRODUCT_TIMEOUT = float(os.getenv("PRODUCT_TIMEOUT_SECONDS", "5"))
PAYMENT_TIMEOUT = float(os.getenv("PAYMENT_TIMEOUT_SECONDS", "10"))

DB_CONNECT_RETRIES = max(1, int(os.getenv("DB_CONNECT_RETRIES", "10")))
DB_CONNECT_RETRY_DELAY_SECONDS = max(0.1, float(os.getenv("DB_CONNECT_RETRY_DELAY_SECONDS", "2")))

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# ==============================================================================
# Database Models (SQLAlchemy ORM)
# ==============================================================================

class Base(DeclarativeBase):
    """Declarative base class for Order Service models."""
    pass


class Order(Base):
    """Represents a customer purchase order and its financial lifecycle."""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    user_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    user_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    tax: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    grand_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PAYMENT_PENDING", index=True)
    payment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    razorpay_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class OrderItem(Base):
    """Line item in an order capturing snapshot product details and pricing."""

    __tablename__ = "order_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sku: Mapped[str] = mapped_column(String(32), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")


# ==============================================================================
# Pydantic Schemas (Request / Response validation)
# ==============================================================================

class OrderLineInput(BaseModel):
    """Specification of an individual product and quantity to be ordered."""
    product_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=36)]
    quantity: int = Field(gt=0, le=10000)


class OrderCreate(BaseModel):
    """Payload submitted to create a new order."""
    items: list[OrderLineInput] = Field(min_length=1, max_length=100)
    user_email: Optional[str] = Field(default=None, max_length=320)
    user_name: Optional[str] = Field(default=None, max_length=255)
    user_address: Optional[str] = Field(default=None, max_length=2000)
    currency: str = Field(default="INR", min_length=3, max_length=3)


class PaymentVerifyInput(BaseModel):
    """Payload containing cryptographic Razorpay payment verification signatures."""
    razorpay_order_id: str = Field(min_length=1, max_length=128)
    razorpay_payment_id: str = Field(min_length=1, max_length=128)
    razorpay_signature: str = Field(min_length=1, max_length=512)


class CancelInput(BaseModel):
    """Payload containing optional reason for order cancellation."""
    reason: Optional[str] = Field(default=None, max_length=500)


# ==============================================================================
# HTTP Client Management & Database Initialization
# ==============================================================================

http_client: httpx.Client | None = None


def get_http_client() -> httpx.Client:
    """Retrieve or initialize the shared HTTP client for outbound service requests."""
    global http_client
    if http_client is None or http_client.is_closed:
        http_client = httpx.Client(timeout=PAYMENT_TIMEOUT)
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
        f"Order Service could not connect to its configured database after "
        f"{DB_CONNECT_RETRIES} attempts. Check DATABASE_URL and database readiness."
    ) from last_error


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Lifespan context manager: initializes DB and manages HTTP client session lifecycle."""
    global http_client
    init_db()
    http_client = httpx.Client(timeout=PAYMENT_TIMEOUT)
    yield
    if http_client and not http_client.is_closed:
        http_client.close()


app = FastAPI(
    title="E-Commerce Order Service",
    description="Order management and payment coordinator",
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


def admin_identity(who: tuple[str, str] = Depends(identity)) -> tuple[str, str]:
    """Require administrator or order manager privileges for administrative actions."""
    if who[1] not in {"ADMIN", "ORDER_MANAGER"}:
        raise HTTPException(status_code=403, detail="Administrator or order-manager role required")
    return who


def _request_headers(user_id: str, role: str) -> dict[str, str]:
    """Construct downstream authentication headers for internal inter-service HTTP calls."""
    return {"X-Auth-User-Id": user_id, "X-Auth-User-Role": role}


def _call(method: str, url: str, *, timeout: float, headers: dict[str, str], **kwargs) -> httpx.Response:
    """Execute an outbound HTTP request with standard timeout and error handling."""
    client = get_http_client()
    try:
        response = client.request(method, url, timeout=timeout, headers=headers, **kwargs)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="A dependent service timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="A dependent service is unavailable") from exc
    return response


def _expect(response: httpx.Response, expected: set[int], service: str) -> dict:
    """Validate that an outbound HTTP response matches expected status codes and return JSON."""
    if response.status_code not in expected:
        try:
            detail = response.json().get("detail", "request failed")
        except Exception:
            detail = "request failed"
        # Preserve meaningful downstream conflict/not-found responses without leaking internals
        mapped = response.status_code if response.status_code in {400, 404, 409, 422} else 502
        raise HTTPException(status_code=mapped, detail={"service": service, "message": detail})

    if not response.content:
        return {}

    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"{service} returned an invalid response") from exc


def _order_payload(order: Order, include_payment: bool = True) -> dict:
    """Serialize an Order and its line items into a standard response dictionary."""
    result = {
        "id": order.id,
        "user_id": order.user_id,
        "user_email": order.user_email,
        "user_name": order.user_name,
        "user_address": order.user_address,
        "items": [
            {
                "product_id": item.product_id,
                "product_name": item.product_name,
                "sku": item.sku,
                "unit_price": str(item.unit_price),
                "quantity": item.quantity,
                "line_total": str(item.line_total),
            }
            for item in order.items
        ],
        "subtotal": str(order.subtotal),
        "tax": str(order.tax),
        "grand_total": str(order.grand_total),
        "currency": order.currency,
        "status": order.status,
        "created_at": order.created_at.isoformat(),
        "updated_at": order.updated_at.isoformat(),
    }
    if include_payment:
        result["payment"] = {
            "payment_id": order.payment_id,
            "razorpay_order_id": order.razorpay_order_id,
        }
    return result


def _create_payment_for_order(order: Order, user_id: str, role: str) -> dict:
    """Initialize a payment with the Payment Service for a pending order."""
    # Avoid duplicate payment orders if a previous successful response was persisted
    if order.payment_id and order.razorpay_order_id:
        return {
            "payment_id": order.payment_id,
            "razorpay_order_id": order.razorpay_order_id,
            "status": "pending",
        }

    response = _call(
        "POST",
        f"{PAYMENT_SERVICE_URL}/payments/create-order",
        timeout=PAYMENT_TIMEOUT,
        headers=_request_headers(user_id, role),
        json={"order_id": order.id, "amount": str(order.grand_total), "currency": order.currency},
    )
    payment = _expect(response, {200, 201}, "payment-service")

    if not payment.get("payment_id") or not payment.get("razorpay_order_id"):
        raise HTTPException(status_code=502, detail="Payment Service returned incomplete payment details")

    return payment


# ==============================================================================
# Endpoints & Route Handlers
# ==============================================================================

@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    """Service health and database probe endpoint."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"status": "DOWN", "service": SERVICE_NAME, "database": "DOWN"},
        ) from exc
    return {"status": "UP", "service": SERVICE_NAME, "database": "UP"}


@app.post("/orders", status_code=status.HTTP_201_CREATED)
def create_order(
    data: OrderCreate,
    db: Session = Depends(get_db),
    who: tuple[str, str] = Depends(identity),
) -> dict:
    """Create a new order, calculate totals, reserve stock, and initialize payment.

    Flow:
    1. Validate currency and merge duplicate product lines.
    2. Fetch live product prices from Product Service and ensure PUBLISHED status.
    3. Persist order with PAYMENT_PENDING status.
    4. Call Product Service to reserve stock against order.id (rollback on failure).
    5. Call Payment Service to create Razorpay payment order.
    """
    user_id, role = who
    currency = data.currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise HTTPException(status_code=422, detail="currency must be a three-letter code")

    # Merge repeated product IDs before fetching prices and reserving stock
    quantities: dict[str, int] = {}
    for line in data.items:
        quantities[line.product_id] = quantities.get(line.product_id, 0) + line.quantity

    products: list[dict] = []
    subtotal = Decimal("0.00")

    for product_id, quantity in quantities.items():
        response = _call(
            "GET",
            f"{PRODUCT_SERVICE_URL}/products/{product_id}",
            timeout=PRODUCT_TIMEOUT,
            headers=_request_headers(user_id, role),
        )
        product = _expect(response, {200}, "product-service")

        if product.get("status") != "PUBLISHED":
            raise HTTPException(status_code=409, detail=f"Product {product_id} is not available for purchase")

        try:
            price = Decimal(str(product["price"])).quantize(Decimal("0.01"))
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Product Service returned an invalid price") from exc

        if price <= 0:
            raise HTTPException(status_code=502, detail="Product Service returned an invalid price")

        line_total = (price * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        subtotal += line_total
        products.append({
            "product_id": product_id,
            "product_name": product["name"],
            "sku": product["sku"],
            "price": price,
            "quantity": quantity,
            "line_total": line_total,
        })

    subtotal = subtotal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    tax = Decimal("0.00")
    grand_total = subtotal + tax

    order = Order(
        user_id=user_id,
        user_email=data.user_email,
        user_name=data.user_name,
        user_address=data.user_address,
        subtotal=subtotal,
        tax=tax,
        grand_total=grand_total,
        currency=currency,
        status="PAYMENT_PENDING",
    )

    for product in products:
        order.items.append(
            OrderItem(
                product_id=product["product_id"],
                product_name=product["product_name"],
                sku=product["sku"],
                unit_price=product["price"],
                quantity=product["quantity"],
                line_total=product["line_total"],
            )
        )

    db.add(order)
    try:
        db.commit()
        db.refresh(order)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Could not persist order") from exc

    # Reserve stock only after durable order ID exists; reservation is idempotent by order ID
    reservation_response = _call(
        "POST",
        f"{PRODUCT_SERVICE_URL}/inventory/reservations",
        timeout=PRODUCT_TIMEOUT,
        headers=_request_headers(user_id, role),
        json={
            "order_id": order.id,
            "items": [{"product_id": p["product_id"], "quantity": p["quantity"]} for p in products],
        },
    )

    try:
        _expect(reservation_response, {200, 201}, "product-service")
    except HTTPException as exc:
        order.status = "CANCELLED"
        db.commit()
        raise exc

    # Attempt payment order creation; on transient failure leave PAYMENT_PENDING for client retry
    try:
        payment = _create_payment_for_order(order, user_id, role)
        order.payment_id = str(payment["payment_id"])
        order.razorpay_order_id = payment["razorpay_order_id"]
        db.commit()
    except HTTPException:
        order.status = "PAYMENT_PENDING"
        db.commit()
        db.refresh(order)
        result = _order_payload(order)
        result["payment_setup"] = "RETRY_REQUIRED"
        return result

    db.refresh(order)
    result = _order_payload(order)
    result["payment_setup"] = "READY"
    result["razorpay_order"] = payment.get("razorpay_order")
    return result


@app.post("/orders/{order_id}/payment")
def retry_payment(
    order_id: str,
    db: Session = Depends(get_db),
    who: tuple[str, str] = Depends(identity),
) -> dict:
    """Retry payment initialization for an order in PAYMENT_PENDING state."""
    user_id, role = who
    order = db.scalar(select(Order).where(Order.id == order_id))
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.user_id != user_id and role not in {"ADMIN", "ORDER_MANAGER"}:
        raise HTTPException(status_code=403, detail="You cannot access this order")

    if order.status == "PAID":
        raise HTTPException(status_code=409, detail="Order is already paid")

    if order.status != "PAYMENT_PENDING":
        raise HTTPException(status_code=409, detail=f"Cannot create payment for order in state {order.status}")

    payment = _create_payment_for_order(order, user_id, role)
    order.payment_id = str(payment["payment_id"])
    order.razorpay_order_id = payment["razorpay_order_id"]

    db.commit()
    db.refresh(order)

    result = _order_payload(order)
    result["razorpay_order"] = payment.get("razorpay_order")
    return result


@app.post("/orders/{order_id}/payment/verify")
def verify_order_payment(
    order_id: str,
    data: PaymentVerifyInput,
    db: Session = Depends(get_db),
    who: tuple[str, str] = Depends(identity),
) -> dict:
    """Verify payment signature with Payment Service and transition order to PAID."""
    user_id, role = who
    order = db.scalar(select(Order).where(Order.id == order_id))
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.user_id != user_id and role not in {"ADMIN", "ORDER_MANAGER"}:
        raise HTTPException(status_code=403, detail="You cannot access this order")

    if order.status == "PAID":
        return _order_payload(order)

    if not order.razorpay_order_id or data.razorpay_order_id != order.razorpay_order_id:
        raise HTTPException(status_code=400, detail="Payment order ID does not match this order")

    response = _call(
        "POST",
        f"{PAYMENT_SERVICE_URL}/payments/verify",
        timeout=PAYMENT_TIMEOUT,
        headers=_request_headers(user_id, role),
        json=data.model_dump(),
    )
    payment = _expect(response, {200}, "payment-service")

    if payment.get("status") != "success":
        raise HTTPException(status_code=409, detail="Payment Service has not confirmed success")

    order.status = "PAID"
    order.payment_id = str(payment.get("payment", {}).get("id", order.payment_id or "")) or order.payment_id

    db.commit()
    db.refresh(order)

    return _order_payload(order)


@app.get("/orders")
def list_orders(
    db: Session = Depends(get_db),
    who: tuple[str, str] = Depends(identity),
) -> dict:
    """List orders for the authenticated customer, or all orders if admin/manager."""
    user_id, role = who
    query = select(Order).order_by(Order.created_at.desc())

    if role not in {"ADMIN", "ORDER_MANAGER"}:
        query = query.where(Order.user_id == user_id)

    orders = db.scalars(query).all()
    return {"items": [_order_payload(order) for order in orders], "count": len(orders)}


@app.get("/orders/{order_id}")
def get_order(
    order_id: str,
    db: Session = Depends(get_db),
    who: tuple[str, str] = Depends(identity),
) -> dict:
    """Retrieve full details of an individual order by ID."""
    user_id, role = who
    order = db.scalar(select(Order).where(Order.id == order_id))
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.user_id != user_id and role not in {"ADMIN", "ORDER_MANAGER"}:
        raise HTTPException(status_code=403, detail="You cannot access this order")

    return _order_payload(order)


@app.post("/orders/{order_id}/cancel")
def cancel_order(
    order_id: str,
    data: CancelInput,
    db: Session = Depends(get_db),
    who: tuple[str, str] = Depends(identity),
) -> dict:
    """Cancel a pending order and release its reserved inventory."""
    user_id, role = who
    order = db.scalar(select(Order).where(Order.id == order_id))
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.user_id != user_id and role not in {"ADMIN", "ORDER_MANAGER"}:
        raise HTTPException(status_code=403, detail="You cannot access this order")

    if order.status == "PAID":
        raise HTTPException(status_code=409, detail="Paid orders cannot be cancelled through this endpoint")

    if order.status == "CANCELLED":
        return _order_payload(order)

    # If payment record exists, ensure it is not already captured
    if order.payment_id:
        response = _call(
            "GET",
            f"{PAYMENT_SERVICE_URL}/payments/{order.payment_id}",
            timeout=PAYMENT_TIMEOUT,
            headers=_request_headers(user_id, role),
        )
        payment = _expect(response, {200}, "payment-service")
        if payment.get("status") == "success":
            raise HTTPException(
                status_code=409,
                detail="Payment is already successful; cancellation requires a refund workflow",
            )

    # Release reserved inventory back into available stock
    release = _call(
        "POST",
        f"{PRODUCT_SERVICE_URL}/inventory/reservations/release",
        timeout=PRODUCT_TIMEOUT,
        headers=_request_headers(user_id, role),
        json={"order_id": order.id},
    )
    _expect(release, {200, 404}, "product-service")

    order.status = "CANCELLED"
    db.commit()
    db.refresh(order)

    return _order_payload(order)
