from typing import Any

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import _redact
from app.main import create_app


def _pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )


async def test_health_ok(client: AsyncClient) -> None:
    r = await client.get("/api/v1/health")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "db": "ok", "redis": "ok"}


async def test_security_headers(client: AsyncClient) -> None:
    r = await client.get("/api/v1/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


async def _client_with_post_route() -> AsyncClient:
    app = create_app()
    r = APIRouter()

    @r.post("/api/v1/_echo")
    async def echo() -> dict[str, bool]:
        return {"ok": True}

    @r.post("/api/v1/payments/_hook")
    async def hook() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(r)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_csrf_requires_custom_header() -> None:
    async with await _client_with_post_route() as c:
        assert (await c.post("/api/v1/_echo")).status_code == 403
        ok = await c.post("/api/v1/_echo", headers={"X-Requested-With": "fetch"})
        assert ok.status_code == 200


async def test_csrf_rejects_foreign_origin() -> None:
    async with await _client_with_post_route() as c:
        r = await c.post(
            "/api/v1/_echo",
            headers={"X-Requested-With": "fetch", "Origin": "https://evil.example"},
        )
        assert r.status_code == 403
        r = await c.post(
            "/api/v1/_echo",
            headers={"X-Requested-With": "fetch", "Origin": "http://localhost:3000"},
        )
        assert r.status_code == 200


async def test_payment_webhooks_skip_csrf() -> None:
    async with await _client_with_post_route() as c:
        assert (await c.post("/api/v1/payments/_hook")).status_code == 200


async def test_docs_visible_outside_production(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/openapi.json")).status_code == 200


def test_docs_hidden_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("ALLOWED_ORIGINS", '["https://food.example.edu"]')
    monkeypatch.setenv("JWT_PRIVATE_KEY", _pem())
    monkeypatch.setenv("PAYMENT_PROVIDER", "razorpay")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "s")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "w")
    config.get_settings.cache_clear()
    try:
        app = create_app()
        paths = {getattr(r, "path", "") for r in app.routes}
        assert "/api/v1/openapi.json" not in paths
        assert "/api/v1/docs" not in paths
    finally:
        config.get_settings.cache_clear()


def test_production_rejects_default_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(env="production")
    with pytest.raises(ValidationError):
        Settings(env="production", secret_key="short")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):  # missing JWT key
        Settings(env="production", secret_key="y" * 40, allowed_origins=["https://a.edu"])  # type: ignore[arg-type]
    prod: dict[str, Any] = {
        "env": "production",
        "secret_key": "y" * 40,
        "allowed_origins": ["https://a.edu"],
        "jwt_private_key": _pem(),
    }
    with pytest.raises(ValidationError):  # fake payments / no Razorpay keys
        Settings(**prod)
    with pytest.raises(ValidationError):  # missing webhook secret
        Settings(**prod, payment_provider="razorpay", razorpay_key_id="k", razorpay_key_secret="s")
    Settings(
        **prod,
        payment_provider="razorpay",
        razorpay_key_id="k",
        razorpay_key_secret="s",
        razorpay_webhook_secret="w",
    )


def test_logs_redact_secrets() -> None:
    out = _redact({"phone": "+919900011100", "otp": "123456", "nested": {"refresh_token": "abc"}})
    assert out == {
        "phone": "+919900011100",
        "otp": "[redacted]",
        "nested": {"refresh_token": "[redacted]"},
    }
    assert "eyJ" not in _redact("token eyJhbGciOi.eyJzdWIi.sig")


def test_startup_error_does_not_leak_db_password() -> None:
    with pytest.raises(ValidationError) as e:
        Settings(env="production", database_url="postgresql+asyncpg://u:hunter2@db/x")
    assert "hunter2" not in str(e.value)
