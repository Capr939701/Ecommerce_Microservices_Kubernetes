import importlib.util
import os
import sys
from pathlib import Path
import sqlalchemy
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-unit-tests-123456")
spec = importlib.util.spec_from_file_location("auth_module", ROOT / "auth-service" / "main.py")
auth = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = auth
_real_create_engine = sqlalchemy.create_engine
sqlalchemy.create_engine = lambda *args, **kwargs: _real_create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
try:
    spec.loader.exec_module(auth)
finally:
    sqlalchemy.create_engine = _real_create_engine


def test_password_hash_and_verify():
    hashed = auth.hash_password("CorrectHorseBatteryStaple!")
    assert auth.check_password("CorrectHorseBatteryStaple!", hashed)
    assert not auth.check_password("wrong-password", hashed)


def test_verification_code_is_hashed():
    code = "12345"
    assert auth.hash_verification_code(code) != code
    assert auth.hash_verification_code(code) == auth.hash_verification_code(code)


def test_jwt_has_subject_and_expiration(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret-that-is-long-enough-for-unit-tests-123456")
    class User:
        id = "user-123"
        name = "Test User"
        email = "test@example.com"
        role = "CUSTOMER"
    token = auth.issue_token(User())
    decoded = auth.decode_token(token)
    assert decoded["sub"] == "user-123"
    assert decoded["role"] == "CUSTOMER"
    assert decoded["exp"] > decoded["iat"]


def test_register_verify_and_login_flow(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(auth, "engine", test_engine)
    monkeypatch.setattr(auth, "SessionLocal", sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False))
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret-that-is-long-enough-for-unit-tests-123456")
    monkeypatch.setattr(auth, "EXPOSE_VERIFICATION_CODE", True)
    auth.Base.metadata.drop_all(bind=test_engine)
    auth.Base.metadata.create_all(bind=test_engine)
    with TestClient(auth.app) as client:
        registered = client.post("/auth/register", json={
            "name": "Test User", "email": "test-flow@example.com", "password": "StrongPassword123"
        })
        assert registered.status_code == 201, registered.text
        code = registered.json()["verificationCode"]
        verified = client.post("/auth/verify-email", json={"email": "test-flow@example.com", "code": code})
        assert verified.status_code == 200, verified.text
        login = client.post("/auth/login", json={"email": "test-flow@example.com", "password": "StrongPassword123"})
        assert login.status_code == 200, login.text
        token = login.json()["token"]
        checked = client.post("/auth/verify-token", json={"token": token})
        assert checked.status_code == 200, checked.text
        assert checked.json()["user"]["email"] == "test-flow@example.com"
    test_engine.dispose()
