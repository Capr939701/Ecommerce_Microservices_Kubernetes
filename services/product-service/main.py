"""Product & Inventory Microservice.

Handles product catalog management, SKU uniqueness, live stock tracking,
pessimistic lock stock reservations (`with_for_update`), and stock releases on order cancellation.
Backed by PostgreSQL (with SQLite compatibility for isolated unit testing).
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Generator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from typing_extensions import Annotated

# ==============================================================================
# Environment Configuration & Database Setup
# ==============================================================================

SERVICE_NAME = "product-service"
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://product_user:product_pass@product-db:5432/product_db",
)

if not (DATABASE_URL.startswith("postgresql+") or DATABASE_URL.startswith("sqlite")):
    raise RuntimeError("DATABASE_URL must point to PostgreSQL; SQLite is not supported for application runtime")

DB_CONNECT_RETRIES = max(1, int(os.getenv("DB_CONNECT_RETRIES", "10")))
DB_CONNECT_RETRY_DELAY_SECONDS = max(0.1, float(os.getenv("DB_CONNECT_RETRY_DELAY_SECONDS", "2")))

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# ==============================================================================
# Database Models (SQLAlchemy ORM)
# ==============================================================================

class Base(DeclarativeBase):
    """Declarative base class for Product Service models."""
    pass


class Product(Base):
    """Product catalog item with SKU, naming, pricing, and lifecycle status."""

    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sku: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PUBLISHED", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # 1:1 relationship with inventory tracking
    inventory: Mapped["Inventory"] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        uselist=False,
    )


class Inventory(Base):
    """Stock level entity linked 1:1 with a Product."""

    __tablename__ = "inventory"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    product_id: Mapped[str] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    sku: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    product: Mapped[Product] = relationship(back_populates="inventory")


class Reservation(Base):
    """Stock reservation record linked to a pending order to prevent over-selling."""

    __tablename__ = "inventory_reservations"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_inventory_reservation_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="RESERVED", nullable=False)  # 'RESERVED' or 'RELEASED'
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    items: Mapped[list["ReservationItem"]] = relationship(
        back_populates="reservation",
        cascade="all, delete-orphan",
    )


class ReservationItem(Base):
    """Line item within an inventory reservation detailing the reserved quantity."""

    __tablename__ = "inventory_reservation_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    reservation_id: Mapped[str] = mapped_column(
        ForeignKey("inventory_reservations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sku: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    reservation: Mapped[Reservation] = relationship(back_populates="items")


# ==============================================================================
# Pydantic Schemas (Request / Response validation)
# ==============================================================================

class ProductCreate(BaseModel):
    """Payload to create a new product in the catalog."""
    sku: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=32, pattern=r"^[A-Za-z0-9._-]+$")]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=255)]
    description: Optional[str] = Field(default=None, max_length=2000)
    price: Decimal = Field(gt=Decimal("0"), max_digits=12, decimal_places=2)
    initial_stock: int = Field(default=0, ge=0, le=2_000_000_000)
    status: str = Field(default="PUBLISHED", pattern=r"^(DRAFT|PUBLISHED|UNPUBLISHED)$")


class ProductUpdate(BaseModel):
    """Payload for partial updates to an existing product."""
    name: Optional[Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=255)]] = None
    description: Optional[str] = Field(default=None, max_length=2000)
    price: Optional[Decimal] = Field(default=None, gt=Decimal("0"), max_digits=12, decimal_places=2)
    status: Optional[str] = Field(default=None, pattern=r"^(DRAFT|PUBLISHED|UNPUBLISHED)$")


class StockUpdate(BaseModel):
    """Payload to explicitly set the inventory stock count."""
    quantity: int = Field(ge=0, le=2_000_000_000)


class ReservationLine(BaseModel):
    """Individual product and quantity in an inventory reservation request."""
    product_id: str
    quantity: int = Field(gt=0, le=2_000_000_000)


class ReservationCreate(BaseModel):
    """Payload to create an inventory reservation for an order."""
    order_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    items: list[ReservationLine] = Field(min_length=1, max_length=100)


class ReleaseRequest(BaseModel):
    """Payload to release an existing inventory reservation."""
    order_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


# ==============================================================================
# Database Initialization & Dependency
# ==============================================================================

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
        f"Product Service could not connect to its configured database after "
        f"{DB_CONNECT_RETRIES} attempts. Check DATABASE_URL and database readiness."
    ) from last_error


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Lifespan context manager: ensures database connectivity and schema readiness on startup."""
    init_db()
    yield


app = FastAPI(
    title="E-Commerce Product & Inventory Service",
    description="Catalog management, inventory levels, and stock reservations",
    version="1.0.0",
    lifespan=lifespan,
)


# ==============================================================================
# Dependencies & Helper Functions
# ==============================================================================

def get_db() -> Generator[Session, None, None]:
    """Dependency yielding a database session with guaranteed closure."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_identity(
    x_auth_user_id: Optional[str] = Header(None),
    x_auth_user_role: Optional[str] = Header(None),
) -> tuple[str, str]:
    """Validate authenticated identity headers injected by the API gateway."""
    if not x_auth_user_id:
        raise HTTPException(status_code=401, detail="Authenticated user identity is required")
    return x_auth_user_id, (x_auth_user_role or "CUSTOMER").upper()


def require_admin(identity: tuple[str, str] = Depends(require_identity)) -> tuple[str, str]:
    """Require administrator or product manager privileges."""
    if identity[1] not in {"ADMIN", "PRODUCT_MANAGER"}:
        raise HTTPException(status_code=403, detail="Administrator or product-manager role required")
    return identity


def product_payload(product: Product, include_description: bool = True) -> dict:
    """Format product model into a standard JSON response dictionary."""
    inv = product.inventory
    result = {
        "id": product.id,
        "sku": product.sku,
        "name": product.name,
        "price": str(product.price),
        "status": product.status,
        "inventoryId": inv.id if inv else None,
        "stock": inv.quantity if inv else 0,
        "stockStatus": "In Stock" if inv and inv.quantity > 0 else "Out of Stock",
        "createdAt": product.created_at.isoformat(),
        "updatedAt": product.updated_at.isoformat(),
    }
    if include_description:
        result["description"] = product.description
    return result


# ==============================================================================
# Endpoints & Route Handlers
# ==============================================================================

@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    """Service health and database probe endpoint."""
    try:
        db.execute(text("SELECT 1"))
        return {"status": "UP", "service": SERVICE_NAME, "database": "UP"}
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail={"status": "DOWN", "service": SERVICE_NAME, "database": "DOWN"},
        )


@app.get("/products")
def list_products(
    search: Optional[str] = Query(default=None, max_length=255),
    status_filter: Optional[str] = Query(default=None, alias="status", pattern=r"^(DRAFT|PUBLISHED|UNPUBLISHED)$"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    """List products with pagination, search, and status filtering."""
    query = select(Product).where(Product.status == "PUBLISHED")
    if status_filter:
        query = select(Product).where(Product.status == status_filter)
    if search:
        query = query.where((Product.name.ilike(f"%{search}%")) | (Product.sku.ilike(f"%{search}%")))

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    products = db.scalars(query.order_by(Product.created_at.desc()).offset(skip).limit(limit)).unique().all()

    return {
        "data": [product_payload(p, include_description=False) for p in products],
        "pagination": {"skip": skip, "limit": limit, "total": total},
    }


@app.post("/products", status_code=status.HTTP_201_CREATED)
def create_product(
    data: ProductCreate,
    db: Session = Depends(get_db),
    _identity: tuple[str, str] = Depends(require_admin),
) -> dict:
    """Create a new product and initialize its corresponding inventory record."""
    if db.scalar(select(Product.id).where(Product.sku == data.sku)):
        raise HTTPException(status_code=409, detail="A product with this SKU already exists")

    product = Product(
        sku=data.sku,
        name=data.name,
        description=data.description,
        price=data.price,
        status=data.status,
    )
    product.inventory = Inventory(sku=data.sku, quantity=data.initial_stock)
    db.add(product)

    try:
        db.commit()
        db.refresh(product)
        return product_payload(product)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A product with this SKU already exists")


@app.get("/products/{product_id}")
def get_product(
    product_id: str,
    db: Session = Depends(get_db),
    x_auth_user_role: Optional[str] = Header(None),
) -> dict:
    """Retrieve full product details. Non-admins cannot view draft/unpublished products."""
    product = db.scalar(select(Product).where(Product.id == product_id))
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # Non-admin / unauthenticated users may only retrieve PUBLISHED products
    role = (x_auth_user_role or "CUSTOMER").upper()
    if product.status != "PUBLISHED" and role not in {"ADMIN", "PRODUCT_MANAGER"}:
        raise HTTPException(status_code=404, detail="Product not found")

    return product_payload(product)


@app.patch("/products/{product_id}")
def update_product(
    product_id: str,
    data: ProductUpdate,
    db: Session = Depends(get_db),
    _identity: tuple[str, str] = Depends(require_admin),
) -> dict:
    """Partially update an existing product."""
    product = db.scalar(select(Product).where(Product.id == product_id))
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(product, key, value)

    try:
        db.commit()
        db.refresh(product)
        return product_payload(product)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Product update conflicts with existing data")


@app.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(
    product_id: str,
    db: Session = Depends(get_db),
    _identity: tuple[str, str] = Depends(require_admin),
) -> Response:
    """Soft-delete a product by marking it UNPUBLISHED to preserve historical order references."""
    product = db.scalar(select(Product).where(Product.id == product_id))
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # Preserve order-history references by marking the product unpublished instead of hard-deleting
    product.status = "UNPUBLISHED"
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/inventory/{product_id}")
def get_inventory(product_id: str, db: Session = Depends(get_db)) -> dict:
    """Look up inventory stock levels by product ID or inventory ID."""
    inventory = db.scalar(
        select(Inventory).where((Inventory.product_id == product_id) | (Inventory.id == product_id))
    )
    if not inventory:
        raise HTTPException(status_code=404, detail="Inventory not found")

    return {
        "id": inventory.id,
        "productId": inventory.product_id,
        "sku": inventory.sku,
        "quantity": inventory.quantity,
        "stockStatus": "In Stock" if inventory.quantity > 0 else "Out of Stock",
        "updatedAt": inventory.updated_at.isoformat(),
    }


@app.put("/inventory/{product_id}")
def update_inventory(
    product_id: str,
    data: StockUpdate,
    db: Session = Depends(get_db),
    _identity: tuple[str, str] = Depends(require_admin),
) -> dict:
    """Update inventory quantity with row-level locking."""
    inventory = db.scalar(
        select(Inventory)
        .where((Inventory.product_id == product_id) | (Inventory.id == product_id))
        .with_for_update()
    )
    if not inventory:
        raise HTTPException(status_code=404, detail="Inventory not found")

    inventory.quantity = data.quantity
    db.commit()
    db.refresh(inventory)

    return {
        "id": inventory.id,
        "productId": inventory.product_id,
        "sku": inventory.sku,
        "quantity": inventory.quantity,
        "stockStatus": "In Stock" if inventory.quantity > 0 else "Out of Stock",
    }


@app.post("/inventory/reservations", status_code=status.HTTP_201_CREATED)
def reserve_inventory(
    data: ReservationCreate,
    db: Session = Depends(get_db),
    _identity: tuple[str, str] = Depends(require_identity),
) -> dict:
    """Atomically reserve inventory for an order using row-level locks (with_for_update).

    Idempotent: If a reservation for order_id already exists and is RESERVED, returns the existing reservation.
    """
    existing = db.scalar(select(Reservation).where(Reservation.order_id == data.order_id))
    if existing:
        if existing.status == "RESERVED":
            return {
                "id": existing.id,
                "orderId": existing.order_id,
                "status": existing.status,
                "items": [{"productId": i.product_id, "sku": i.sku, "quantity": i.quantity} for i in existing.items],
                "idempotent": True,
            }
        raise HTTPException(status_code=409, detail=f"Order inventory reservation is already {existing.status.lower()}")

    # Aggregate duplicate product lines before checking or decrementing stock
    totals: dict[str, int] = {}
    for item in data.items:
        totals[item.product_id] = totals.get(item.product_id, 0) + item.quantity

    products: list[tuple[Product, Inventory, int]] = []
    for product_id, qty in totals.items():
        product = db.scalar(select(Product).where(Product.id == product_id, Product.status == "PUBLISHED"))
        if not product or not product.inventory:
            db.rollback()
            raise HTTPException(status_code=404, detail=f"Published product or inventory not found: {product_id}")

        inventory = db.scalar(select(Inventory).where(Inventory.product_id == product_id).with_for_update())
        if inventory.quantity < qty:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail={"message": "Insufficient stock", "productId": product_id, "available": inventory.quantity, "requested": qty},
            )
        products.append((product, inventory, qty))

    reservation = Reservation(order_id=data.order_id, status="RESERVED")
    for product, inventory, qty in products:
        inventory.quantity -= qty
        reservation.items.append(ReservationItem(product_id=product.id, sku=product.sku, quantity=qty))

    db.add(reservation)
    try:
        db.commit()
        db.refresh(reservation)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A reservation already exists for this order")

    return {
        "id": reservation.id,
        "orderId": reservation.order_id,
        "status": reservation.status,
        "items": [{"productId": i.product_id, "sku": i.sku, "quantity": i.quantity} for i in reservation.items],
        "idempotent": False,
    }


@app.post("/inventory/reservations/release")
def release_inventory(
    data: ReleaseRequest,
    db: Session = Depends(get_db),
    _identity: tuple[str, str] = Depends(require_identity),
) -> dict:
    """Release reserved stock back to the active inventory upon order cancellation."""
    reservation = db.scalar(
        select(Reservation).where(Reservation.order_id == data.order_id).with_for_update()
    )
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")

    if reservation.status == "RELEASED":
        return {"orderId": reservation.order_id, "status": "RELEASED", "idempotent": True}

    for item in reservation.items:
        inventory = db.scalar(select(Inventory).where(Inventory.product_id == item.product_id).with_for_update())
        if inventory:
            inventory.quantity += item.quantity

    reservation.status = "RELEASED"
    db.commit()

    return {"orderId": reservation.order_id, "status": reservation.status, "idempotent": False}


@app.get("/inventory/reservations/{order_id}")
def get_reservation(
    order_id: str,
    db: Session = Depends(get_db),
    _identity: tuple[str, str] = Depends(require_identity),
) -> dict:
    """Retrieve details of an existing inventory reservation by order ID."""
    reservation = db.scalar(select(Reservation).where(Reservation.order_id == order_id))
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")

    return {
        "id": reservation.id,
        "orderId": reservation.order_id,
        "status": reservation.status,
        "items": [{"productId": i.product_id, "sku": i.sku, "quantity": i.quantity} for i in reservation.items],
    }
