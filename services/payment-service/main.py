"""Razorpay-backed payment microservice.

PostgreSQL is the intended runtime database. The service deliberately does not
fall back to SQLite if the configured database is unavailable.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Generator

import razorpay
import razorpay.errors
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import DateTime, Index, Integer, Numeric, String, create_engine, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
import uvicorn

logger = logging.getLogger(__name__)

load_dotenv()

PORT = int(os.getenv("PORT", "8003"))
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg2://payment_user:payment_pass@payment-db:5432/payment_db")
DB_CONNECT_RETRIES = max(1, int(os.getenv("DB_CONNECT_RETRIES", "10")))
DB_CONNECT_RETRY_DELAY_SECONDS = max(0.1, float(os.getenv("DB_CONNECT_RETRY_DELAY_SECONDS", "2")))
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")

# SQLAlchemy engine creation does not prove the DB is reachable. Connectivity is
# checked during startup and by /health; no alternate database is substituted.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class PaymentRecord(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    # One payment lifecycle per e-commerce order. This DB constraint also guards
    # concurrent create-order requests from inserting duplicate records.
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
# payment record for an order at a time. PostgreSQL and SQLite support this index.
Index(
    "uq_payments_active_order",
    PaymentRecord.order_id,
    unique=True,
    postgresql_where=text("status IN ('pending', 'success')"),
    sqlite_where=text("status IN ('pending', 'success')"),
)


def initialize_database() -> None:
    """Wait briefly for the configured database, then fail startup clearly."""
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
        except Exception as exc:  # startup must retry transient DB readiness failures
            last_error = exc
            if attempt < DB_CONNECT_RETRIES:
                time.sleep(DB_CONNECT_RETRY_DELAY_SECONDS)
    raise RuntimeError(
        f"Payment Service could not connect to its configured database after "
        f"{DB_CONNECT_RETRIES} attempts. Check DATABASE_URL and database readiness."
    ) from last_error


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_identity(
    x_auth_user_id: str | None = Header(None),
    x_auth_user_role: str | None = Header(None),
) -> tuple[str, str]:
    if not x_auth_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user identity is required",
        )
    return x_auth_user_id, (x_auth_user_role or "CUSTOMER").upper()


def validate_configuration() -> None:
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RuntimeError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be configured")


razorpay_client: Any = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_configuration()
    initialize_database()
    yield


app = FastAPI(
    title="FastAPI Payment Service",
    description="Payment Service with Razorpay integration and database persistence",
    version="1.1.0",
    lifespan=lifespan,
)


class CreateOrderRequest(BaseModel):
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
    razorpay_order_id: str = Field(min_length=1, max_length=128)
    razorpay_payment_id: str = Field(min_length=1, max_length=128)
    razorpay_signature: str = Field(min_length=1, max_length=512)


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_id: str
    amount: Decimal
    currency: str
    razorpay_order_id: str
    razorpay_payment_id: str | None
    status: str
    created_at: datetime


def _payment_payload(payment: PaymentRecord) -> dict[str, Any]:
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


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")
    return {"status": "healthy", "database": "connected", "port": PORT}


@app.post("/payments/create-order", status_code=status.HTTP_201_CREATED)
def create_order(
    payload: CreateOrderRequest,
    db: Session = Depends(get_db),
    _who: tuple[str, str] = Depends(require_identity),
) -> dict[str, Any]:
    existing = db.query(PaymentRecord).filter(
        PaymentRecord.order_id == payload.order_id, PaymentRecord.status.in_(["pending", "success"])
    ).order_by(PaymentRecord.id.desc()).first()
    if existing:
        if existing.status == "success":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Order has already been paid")
        if existing.amount != payload.amount or existing.currency != payload.currency:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A payment already exists for this order with a different amount or currency",
            )
        # Idempotent retry: do not create another Razorpay order for the same order.
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
        # Log diagnostic metadata without exposing credentials or response bodies.
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
        # A concurrent request may have inserted this order after our initial read.
        db.rollback()
        existing = db.query(PaymentRecord).filter(
            PaymentRecord.order_id == payload.order_id, PaymentRecord.status.in_(["pending", "success"])
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
    payment = db.query(PaymentRecord).filter(
        PaymentRecord.razorpay_order_id == payload.razorpay_order_id
    ).first()
    if not payment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment record not found")

    # Success is terminal here: a later malformed/replayed request cannot undo it.
    if payment.status == "success":
        return {"status": "success", "message": "Payment was already verified", "payment": _payment_payload(payment)}

    try:
        razorpay_client.utility.verify_payment_signature({
            "razorpay_order_id": payload.razorpay_order_id,
            "razorpay_payment_id": payload.razorpay_payment_id,
            "razorpay_signature": payload.razorpay_signature,
        })
    except razorpay.errors.SignatureVerificationError:
        # Do not mark the stored payment failed: an invalid request must not be
        # able to alter the record or prevent the customer from retrying correctly.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment signature verification failed")
    except Exception:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Payment verification could not be completed")

    payment.razorpay_payment_id = payload.razorpay_payment_id
    payment.status = "success"
    try:
        db.commit()
        db.refresh(payment)
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not save payment verification")

    return {"status": "success", "message": "Payment verified successfully", "payment": _payment_payload(payment)}


@app.get("/payments/{payment_id}")
def get_payment(
    payment_id: int,
    db: Session = Depends(get_db),
    _who: tuple[str, str] = Depends(require_identity),
) -> dict[str, Any]:
    payment = db.query(PaymentRecord).filter(PaymentRecord.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment record not found")
    return _payment_payload(payment)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
