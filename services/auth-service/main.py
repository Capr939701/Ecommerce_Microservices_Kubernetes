from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Generator, Optional

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, create_engine, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker, relationship

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg2://auth_user:auth_pass@auth-db:5432/auth_db")
if not DATABASE_URL.startswith("postgresql+"):
    raise RuntimeError("DATABASE_URL must point to PostgreSQL; SQLite is not supported for application runtime")
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))
EXPOSE_VERIFICATION_CODE = os.getenv("AUTH_EXPOSE_VERIFICATION_CODE", "false").lower() == "true"

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False, default="CUSTOMER")
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    verification_codes: Mapped[list["VerificationCode"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    profile: Mapped[Optional["UserProfile"]] = relationship(back_populates="user", cascade="all, delete-orphan", uselist=False)

class VerificationCode(Base):
    __tablename__ = "verification_codes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    user: Mapped[User] = relationship(back_populates="verification_codes")

class LoginHistory(Base):
    __tablename__ = "login_history"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    attempt: Mapped[str] = mapped_column(String(10), nullable=False)
    ip: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    user_agent: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

class UserProfile(Base):
    __tablename__ = "user_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    auth_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    user: Mapped[User] = relationship(back_populates="profile")

app = FastAPI(title="User/Auth Service", version="2.0.0")

def now() -> datetime:
    return datetime.now(timezone.utc)

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db() -> None:
    if not JWT_SECRET or len(JWT_SECRET) < 32:
        raise RuntimeError("JWT_SECRET must be configured and contain at least 32 characters")
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    Base.metadata.create_all(bind=engine)

@app.on_event("startup")
def startup() -> None:
    init_db()

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + "$" + digest.hex()

def check_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False

def issue_token(user: User) -> str:
    issued_at = now()
    payload = {"sub": user.id, "id": user.id, "name": user.name, "email": user.email,
               "role": user.role, "iat": issued_at, "exp": issued_at + timedelta(minutes=JWT_EXPIRE_MINUTES)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc

def hash_verification_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()

class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
class LoginIn(BaseModel):
    email: EmailStr
    password: str
class VerifyEmailIn(BaseModel):
    email: EmailStr
    code: str = Field(min_length=5, max_length=10)
class TokenIn(BaseModel):
    token: str
class UserProfileIn(BaseModel):
    authUserId: str
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr

def _user_public(user: User) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role,
            "status": user.status, "verified": user.verified}

@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "UP", "service": "user-auth", "database": "UP"}
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Auth PostgreSQL database unavailable") from exc

@app.post("/auth/register", status_code=201)
def register(data: RegisterIn, db: Session = Depends(get_db)):
    normalized_email = str(data.email).lower()
    current = now()
    code = f"{secrets.randbelow(90000) + 10000}"
    user = User(name=data.name.strip(), email=normalized_email, password_hash=hash_password(data.password),
                role="CUSTOMER", verified=False, status="PENDING", created_at=current)
    user.profile = UserProfile(name=data.name.strip(), email=normalized_email)
    user.verification_codes.append(VerificationCode(code_hash=hash_verification_code(code),
        expires_at=current + timedelta(hours=24), used=False, created_at=current))
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email already exists") from exc
    result = {"message": "Account created. Verify the email address before logging in.",
              "user": {**_user_public(user), "createdAt": user.created_at.isoformat()}}
    if EXPOSE_VERIFICATION_CODE:
        result["verificationCode"] = code
    return result

@app.post("/auth/verify-email")
def verify_email(data: VerifyEmailIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == str(data.email).lower()))
    if not user:
        raise HTTPException(status_code=400, detail="Invalid verification details")
    code_row = db.scalar(select(VerificationCode).where(VerificationCode.user_id == user.id)
                         .order_by(VerificationCode.created_at.desc()).limit(1))
    if not code_row or code_row.used or not hmac.compare_digest(code_row.code_hash, hash_verification_code(data.code)):
        raise HTTPException(status_code=400, detail="Invalid or already-used verification code")
    expiry = code_row.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry < now():
        raise HTTPException(status_code=400, detail="Verification code expired")
    user.verified = True
    user.status = "ACTIVE"
    code_row.used = True
    db.commit()
    return {"message": "Email verified successfully"}

@app.post("/auth/login")
def login(data: LoginIn, x_forwarded_for: Optional[str] = Header(None), user_agent: Optional[str] = Header(None), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == str(data.email).lower()))
    password_ok = bool(user and check_password(data.password, user.password_hash))
    success = bool(password_ok and user.verified and user.status == "ACTIVE")
    db.add(LoginHistory(user_id=user.id if user else None, attempt="SUCCESS" if success else "FAILED",
                        ip=(x_forwarded_for or "")[:255], user_agent=(user_agent or "")[:1000], created_at=now()))
    db.commit()
    if not user or not password_ok:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.verified or user.status != "ACTIVE":
        raise HTTPException(status_code=403, detail="Account must be verified and active before login")
    return {"message": "Login successful", "token": issue_token(user), "tokenType": "Bearer", "expiresIn": JWT_EXPIRE_MINUTES * 60}

@app.post("/auth/verify-token")
def verify_token(data: TokenIn, db: Session = Depends(get_db)):
    decoded = decode_token(data.token)
    user = db.get(User, decoded.get("sub") or decoded.get("id"))
    if not user or user.status != "ACTIVE" or not user.verified:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"user": _user_public(user)}

@app.post("/users", status_code=201)
def create_user_profile(data: UserProfileIn, x_auth_user_id: Optional[str] = Header(None), x_auth_user_role: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not x_auth_user_id:
        raise HTTPException(status_code=401, detail="Authenticated user identity is required")
    if data.authUserId != x_auth_user_id and (x_auth_user_role or "CUSTOMER").upper() != "ADMIN":
        raise HTTPException(status_code=403, detail="You cannot create a profile for another account")
    user = db.get(User, data.authUserId)
    if not user:
        raise HTTPException(status_code=404, detail="Authentication user not found")
    if user.profile:
        raise HTTPException(status_code=409, detail="A profile already exists for this account")
    profile = UserProfile(auth_user_id=user.id, name=data.name.strip(), email=str(data.email).lower())
    db.add(profile)
    try:
        db.commit()
        db.refresh(profile)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A profile already exists for this account") from exc
    return {"message": "User profile created", "data": {"id": profile.id, "authUserId": profile.auth_user_id, "name": profile.name, "email": profile.email}}

@app.get("/users/{user_id}")
def get_user_profile(user_id: str, field: Optional[str] = Query(None), x_auth_user_id: Optional[str] = Header(None), x_auth_user_role: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if field not in (None, "authUserId"):
        raise HTTPException(status_code=400, detail="Unsupported lookup field")
    if not x_auth_user_id:
        raise HTTPException(status_code=401, detail="Authenticated user identity is required")
    lookup_auth_id = user_id if field == "authUserId" else None
    profile = db.scalar(select(UserProfile).where(UserProfile.auth_user_id == lookup_auth_id)) if lookup_auth_id else db.get(UserProfile, user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="User not found")
    if profile.auth_user_id != x_auth_user_id and (x_auth_user_role or "CUSTOMER").upper() != "ADMIN":
        raise HTTPException(status_code=403, detail="You cannot access another user's profile")
    return {"user": {"id": profile.id, "authUserId": profile.auth_user_id, "name": profile.name, "email": profile.email}}
