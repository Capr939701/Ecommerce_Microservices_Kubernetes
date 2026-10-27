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

SERVICE_NAME = "cart-service"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg2://cart_user:cart_pass@cart-db:5432/cart_db")
if not DATABASE_URL.startswith("postgresql+"):
    raise RuntimeError("DATABASE_URL must point to PostgreSQL; SQLite is not supported for application runtime")
PRODUCT_SERVICE_URL = os.getenv("PRODUCT_SERVICE_URL", "http://product-service:8002").rstrip("/")
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service:8004").rstrip("/")
PRODUCT_TIMEOUT = float(os.getenv("PRODUCT_TIMEOUT_SECONDS", "5"))
ORDER_TIMEOUT = float(os.getenv("ORDER_TIMEOUT_SECONDS", "15"))
DB_CONNECT_RETRIES = max(1, int(os.getenv("DB_CONNECT_RETRIES", "10")))
DB_CONNECT_RETRY_DELAY_SECONDS = max(0.1, float(os.getenv("DB_CONNECT_RETRY_DELAY_SECONDS", "2")))
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_cart_user_product"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

class CartItemAdd(BaseModel):
    product_id: str = Field(min_length=1, max_length=36)
    quantity: int = Field(default=1, gt=0, le=10000)
class CartItemUpdate(BaseModel):
    quantity: int = Field(gt=0, le=10000)
class CheckoutRequest(BaseModel):
    currency: str = Field(default="INR", min_length=3, max_length=3)

http_client: httpx.Client | None = None


def get_http_client() -> httpx.Client:
    global http_client
    if http_client is None or http_client.is_closed:
        http_client = httpx.Client(timeout=ORDER_TIMEOUT)
    return http_client


def init_db() -> None:
    if str(DATABASE_URL).startswith("sqlite"):
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
    global http_client
    init_db()
    http_client = httpx.Client(timeout=ORDER_TIMEOUT)
    yield
    if http_client and not http_client.is_closed:
        http_client.close()


app = FastAPI(title="E-Commerce Cart Service", version="1.0.0", lifespan=lifespan)

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def identity(x_auth_user_id: Optional[str] = Header(None), x_auth_user_role: Optional[str] = Header(None)) -> tuple[str, str]:
    if not x_auth_user_id:
        raise HTTPException(status_code=401, detail="Authenticated user identity is required")
    return x_auth_user_id, (x_auth_user_role or "CUSTOMER").upper()

def fetch_product(product_id: str, user_id: str, role: str) -> dict:
    client = get_http_client()
    try:
        response = client.get(f"{PRODUCT_SERVICE_URL}/products/{product_id}", timeout=PRODUCT_TIMEOUT,
                              headers={"X-Auth-User-Id": user_id, "X-Auth-User-Role": role})
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
    return db.scalar(select(CartItem).where(CartItem.user_id == user_id, CartItem.product_id == product_id))

@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    try:
        db.execute(text("SELECT 1"))
        return {"status": "UP", "service": SERVICE_NAME, "database": "UP"}
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail={"status": "DOWN", "service": SERVICE_NAME, "database": "DOWN"}) from exc

@app.post("/cart/items", status_code=201)
def add_to_cart(item: CartItemAdd, db: Session = Depends(get_db), who: tuple[str, str] = Depends(identity)) -> dict:
    user_id, role = who
    product = fetch_product(item.product_id, user_id, role)
    if product.get("status") != "PUBLISHED":
        raise HTTPException(status_code=409, detail="Product is not available for purchase")
    existing = find_item(db, user_id, item.product_id)
    new_quantity = item.quantity + (existing.quantity if existing else 0)
    if int(product.get("stock", 0)) < new_quantity:
        raise HTTPException(status_code=409, detail=f"Not enough stock. Available: {product.get('stock', 0)}, requested: {new_quantity}")
    if existing:
        existing.quantity = new_quantity
    else:
        db.add(CartItem(user_id=user_id, product_id=item.product_id, quantity=item.quantity))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Cart item conflicted with another update; retry the request") from exc
    return {"message": f"{product['name']} added to cart", "product_id": item.product_id, "quantity_in_cart": new_quantity}

@app.get("/cart")
def view_cart(db: Session = Depends(get_db), who: tuple[str, str] = Depends(identity)) -> dict:
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
            items.append({"product_id": row.product_id, "quantity": row.quantity, "available": False, "note": "Product no longer exists"})
            continue
        price = Decimal(str(product["price"]))
        subtotal = (price * row.quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        total += subtotal
        items.append({"product_id": row.product_id, "name": product["name"], "sku": product.get("sku"),
                      "price": str(price), "quantity": row.quantity, "subtotal": str(subtotal),
                      "in_stock": int(product.get("stock", 0)) >= row.quantity, "available": True})
    return {"items": items, "total": str(total.quantize(Decimal('0.01'))), "count": len(items)}

@app.put("/cart/items/{product_id}")
def update_item(product_id: str, updated: CartItemUpdate, db: Session = Depends(get_db), who: tuple[str, str] = Depends(identity)) -> dict:
    user_id, role = who
    row = find_item(db, user_id, product_id)
    if not row:
        raise HTTPException(status_code=404, detail="Item not in cart")
    product = fetch_product(product_id, user_id, role)
    if int(product.get("stock", 0)) < updated.quantity:
        raise HTTPException(status_code=409, detail=f"Not enough stock. Available: {product.get('stock', 0)}, requested: {updated.quantity}")
    row.quantity = updated.quantity
    db.commit()
    return {"message": "Cart quantity updated", "product_id": product_id, "quantity": updated.quantity}

@app.delete("/cart/items/{product_id}", status_code=200)
def remove_item(product_id: str, db: Session = Depends(get_db), who: tuple[str, str] = Depends(identity)) -> dict:
    user_id, _role = who
    row = find_item(db, user_id, product_id)
    if not row:
        raise HTTPException(status_code=404, detail="Item not in cart")
    db.delete(row)
    db.commit()
    return {"message": "Item removed from cart"}

@app.delete("/cart", status_code=200)
def clear_cart(db: Session = Depends(get_db), who: tuple[str, str] = Depends(identity)) -> dict:
    user_id, _role = who
    db.query(CartItem).filter(CartItem.user_id == user_id).delete(synchronize_session=False)
    db.commit()
    return {"message": "Cart cleared"}

@app.post("/cart/checkout", status_code=201)
def checkout(details: CheckoutRequest, db: Session = Depends(get_db), who: tuple[str, str] = Depends(identity)) -> dict:
    user_id, role = who
    rows = db.scalars(select(CartItem).where(CartItem.user_id == user_id).order_by(CartItem.id)).all()
    if not rows:
        raise HTTPException(status_code=400, detail="Cart is empty")
    payload = {"items": [{"product_id": row.product_id, "quantity": row.quantity} for row in rows],
               "currency": details.currency.upper()}
    client = get_http_client()
    try:
        response = client.post(f"{ORDER_SERVICE_URL}/orders", json=payload, timeout=ORDER_TIMEOUT,
                               headers={"X-Auth-User-Id": user_id, "X-Auth-User-Role": role})
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Order Service timed out; check order history before retrying") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Order Service is unavailable") from exc
    if response.status_code not in {200, 201}:
        try:
            detail = response.json().get("detail", "Order creation failed")
        except Exception:
            detail = "Order creation failed"
        raise HTTPException(status_code=response.status_code if response.status_code in {400, 404, 409, 422} else 502, detail=detail)
    order = response.json()
    # Only clear cart after the Order Service confirms that an order was durably created.
    db.query(CartItem).filter(CartItem.user_id == user_id).delete(synchronize_session=False)
    db.commit()
    return {"status": "success", "order": order, "message": "Checkout submitted; continue payment using the returned order details"}
