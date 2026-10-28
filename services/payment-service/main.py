"""Payment Microservice with Razorpay Integration.

Responsible for:
  1. Managing payment records in persistent PostgreSQL storage.
  2. Integration with Razorpay API to generate upstream order IDs (`order_...`).
  3. Strict idempotency: Multiple create-order requests for the same Order ID return
     the existing active payment without duplicating Razorpay transactions.
  4. Cryptographic HMAC-SHA256 signature verification for callback authorization.
  5. Terminal success states: Once marked 'success', payment records are immutable
     and cannot be reverted or overwritten by replayed requests.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Generator

import razorpay
import razorpay.errors
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import DateTime, Index, Integer, Numeric, String, create_engine, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


# =====================================================================
# Configuration and Logging
# =====================================================================

logger = logging.getLogger(__name__)

load_dotenv()

PORT = int(os.getenv("PORT", "8003"))
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://payment_user:payment_pass@payment-db:5432/payment_db",
)
DB_CONNECT_RETRIES = max(1, int(os.getenv("DB_CONNECT_RETRIES", "10")))
DB_CONNECT_RETRY_DELAY_SECONDS = max(0.1, float(os.getenv("DB_CONNECT_RETRY_DELAY_SECONDS", "2")))

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")


# =====================================================================
# Database Setup and Declarative Base
# =====================================================================

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base class for Payment Service SQLAlchemy ORM models."""
    pass


# =====================================================================
# SQLAlchemy Data Models
# =====================================================================

class PaymentRecord(Base):
    """Durable record of an individual payment attempt and its Razorpay transaction state."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    razorpay_order_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


# Allow a new attempt after a failed payment, but only one pending/successful
# payment record for an order at a time. Supported across PostgreSQL and SQLite.
Index(
    "uq_payments_active_order",
    PaymentRecord.order_id,
    unique=True,
    postgresql_where=text("status IN ('pending', 'success')"),
    sqlite_where=text("status IN ('pending', 'success')"),
)


# =====================================================================
# Pydantic Schemas for Request and Response Validation
# =====================================================================

class CreateOrderRequest(BaseModel):
    """Schema for initiating payment order creation."""
    order_id: str = Field(min_length=1, max_length=128)
    amount: Decimal = Field(gt=Decimal("0"), max_digits=12, decimal_places=2)
    currency: str = Field(default="INR", min_length=3, max_length=3)

    @field_validator("order_id")
    @classmethod
    def strip_order_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("order_id must not be blank")
        return value

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("currency must be a three-letter currency code")
        return value


class VerifyPaymentRequest(BaseModel):
    """Schema for submitting Razorpay HMAC-SHA256 signature details."""
    razorpay_order_id: str = Field(min_length=1, max_length=128)
    razorpay_payment_id: str = Field(min_length=1, max_length=128)
    razorpay_signature: str = Field(min_length=1, max_length=512)


class PaymentOut(BaseModel):
    """Schema representing payment output details."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_id: str
    amount: Decimal
    currency: str
    razorpay_order_id: str
    razorpay_payment_id: str | None
    status: str
    created_at: datetime


# =====================================================================
# Database Initialization and Dependencies
# =====================================================================

def initialize_database() -> None:
    """Wait for PostgreSQL database readiness and apply schema migrations."""
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
        f"Payment Service could not connect to its configured database after "
        f"{DB_CONNECT_RETRIES} attempts. Check DATABASE_URL and database readiness."
    ) from last_error


def get_db() -> Generator[Session, None, None]:
    """Yield a database session and ensure clean disposal."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_identity(
    x_auth_user_id: str | None = Header(None),
    x_auth_user_role: str | None = Header(None),
) -> tuple[str, str]:
    """Extract authenticated identity injected by the API gateway."""
    if not x_auth_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user identity is required",
        )
    return x_auth_user_id, (x_auth_user_role or "CUSTOMER").upper()


def validate_configuration() -> None:
    """Validate presence of Razorpay API credentials."""
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RuntimeError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be configured")


razorpay_client: Any = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


# =====================================================================
# Application Lifecycle and FastAPI Setup
# =====================================================================

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Lifecycle manager validating credentials and initializing database."""
    validate_configuration()
    initialize_database()
    yield


app = FastAPI(
    title="FastAPI Payment Service",
    description="Payment Service with Razorpay integration and database persistence",
    version="1.1.0",
    lifespan=lifespan,
)


def _payment_payload(payment: PaymentRecord) -> dict[str, Any]:
    """Serialize a PaymentRecord into a dictionary."""
    return {
        "id": payment.id,
        "order_id": payment.order_id,
        "amount": payment.amount,
        "currency": payment.currency,
        "razorpay_order_id": payment.razorpay_order_id,
        "razorpay_payment_id": payment.razorpay_payment_id,
        "status": payment.status,
        "created_at": payment.created_at,
    }


def _create_order_response(payment: PaymentRecord, razorpay_order: dict[str, Any] | None = None) -> dict[str, Any]:
    """Construct standardized response payload for order creation."""
    response = {
        "payment_id": payment.id,
        "order_id": payment.order_id,
        "amount": payment.amount,
        "currency": payment.currency,
        "razorpay_order_id": payment.razorpay_order_id,
        "status": payment.status,
    }
    if razorpay_order is not None:
        response["razorpay_order"] = razorpay_order
    return response


# =====================================================================
# Route Handlers
# =====================================================================

@app.get("/health")
def health() -> dict[str, Any]:
    """Health check endpoint validating PostgreSQL database connection."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable",
        )
    return {"status": "healthy", "database": "connected", "port": PORT}


@app.post("/payments/create-order", status_code=status.HTTP_201_CREATED)
def create_order(
    payload: CreateOrderRequest,
    db: Session = Depends(get_db),
    _who: tuple[str, str] = Depends(require_identity),
) -> dict[str, Any]:
    """Create a new payment order with Razorpay or return existing active order idempotently."""
    existing = db.query(PaymentRecord).filter(
        PaymentRecord.order_id == payload.order_id,
        PaymentRecord.status.in_(["pending", "success"]),
    ).order_by(PaymentRecord.id.desc()).first()

    if existing:
        if existing.status == "success":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Order has already been paid")
        if existing.amount != payload.amount or existing.currency != payload.currency:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A payment already exists for this order with a different amount or currency",
            )
        # Idempotent retry: return existing Razorpay order details without duplication
        return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder(_create_order_response(existing)))

    amount_in_paise = int((payload.amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    try:
        razorpay_order = razorpay_client.order.create(data={
            "amount": amount_in_paise,
            "currency": payload.currency,
            "receipt": f"order_rcptid_{payload.order_id}",
            "notes": {"order_id": payload.order_id},
        })
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        logger.error(
            "Razorpay order creation failed; exception_type=%s; upstream_status=%s",
            type(exc).__name__,
            status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create Razorpay order",
        ) from None

    payment = PaymentRecord(
        order_id=payload.order_id,
        amount=payload.amount,
        currency=payload.currency,
        razorpay_order_id=razorpay_order["id"],
        status="pending",
    )

    try:
        db.add(payment)
        db.commit()
        db.refresh(payment)
    except IntegrityError:
        # A concurrent request may have inserted this order after our initial read
        db.rollback()
        existing = db.query(PaymentRecord).filter(
            PaymentRecord.order_id == payload.order_id,
            PaymentRecord.status.in_(["pending", "success"]),
        ).order_by(PaymentRecord.id.desc()).first()

        if existing:
            if existing.status == "success":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Order has already been paid")
            if existing.amount == payload.amount and existing.currency == payload.currency:
                return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder(_create_order_response(existing)))
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Conflicting payment already exists")

        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not save payment record")
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not save payment record")

    return _create_order_response(payment, razorpay_order)


@app.post("/payments/verify")
def verify_payment(
    payload: VerifyPaymentRequest,
    db: Session = Depends(get_db),
    _who: tuple[str, str] = Depends(require_identity),
) -> dict[str, Any]:
    """Verify Razorpay payment HMAC-SHA256 signature and mark record as 'success'."""
    payment = db.query(PaymentRecord).filter(
        PaymentRecord.razorpay_order_id == payload.razorpay_order_id
    ).first()
    if not payment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment record not found")

    # Terminal state: already verified payments are permanently immutable
    if payment.status == "success":
        return {
            "status": "success",
            "message": "Payment was already verified",
            "payment": _payment_payload(payment),
        }

    try:
        razorpay_client.utility.verify_payment_signature({
            "razorpay_order_id": payload.razorpay_order_id,
            "razorpay_payment_id": payload.razorpay_payment_id,
            "razorpay_signature": payload.razorpay_signature,
        })
    except razorpay.errors.SignatureVerificationError:
        # Do not mark the stored payment failed: an invalid request must not alter
        # the record or prevent the customer from retrying correctly.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payment signature verification failed",
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment verification could not be completed",
        )

    payment.razorpay_payment_id = payload.razorpay_payment_id
    payment.status = "success"

    try:
        db.commit()
        db.refresh(payment)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not save payment verification",
        )

    return {
        "status": "success",
        "message": "Payment verified successfully",
        "payment": _payment_payload(payment),
    }


@app.get("/payments/{payment_id}")
def get_payment(
    payment_id: int,
    db: Session = Depends(get_db),
    _who: tuple[str, str] = Depends(require_identity),
) -> dict[str, Any]:
    """Retrieve payment attempt record by its primary ID."""
    payment = db.query(PaymentRecord).filter(PaymentRecord.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment record not found")
    return _payment_payload(payment)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
