"""Crypto helpers: JWT (Ed25519), password hashing (argon2id), secret
encryption at rest (AES-GCM), keyed hashes for OTPs / refresh tokens."""

import base64
import hashlib
import hmac
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import get_settings

Audience = Literal["student", "staff"]
ISSUER = "pesu-food-court"
ALGORITHM = "EdDSA"


# ---------- key derivation ----------
def _subkey(purpose: bytes) -> bytes:
    secret = get_settings().secret_key.get_secret_value().encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose).derive(secret)


@lru_cache
def _otp_key() -> bytes:
    return _subkey(b"pfc/otp-hmac/v1")


@lru_cache
def _enc_key() -> bytes:
    return _subkey(b"pfc/secret-enc/v1")


# ---------- keyed hashes ----------
def otp_digest(phone: str, code: str) -> str:
    """HMAC of phone+code: Redis never holds a usable OTP."""
    return hmac.new(_otp_key(), f"{phone}:{code}".encode(), hashlib.sha256).hexdigest()


def token_digest(token: str) -> str:
    # Refresh tokens are 256-bit random, so a plain SHA-256 is enough.
    return hashlib.sha256(token.encode()).hexdigest()


def constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def new_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


# ---------- passwords ----------
_ph = PasswordHasher()  # argon2id, library defaults (RFC 9106 low-memory profile)
_DUMMY_HASH = _ph.hash("timing-equaliser-not-a-password")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(hash_: str | None, password: str) -> bool:
    """Always does one argon2 verify, even for unknown users (no timing oracle)."""
    try:
        return _ph.verify(hash_ or _DUMMY_HASH, password) and hash_ is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ---------- encryption at rest (TOTP secrets) ----------
def encrypt_secret(plain: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_enc_key()).encrypt(nonce, plain.encode(), b"pfc-totp")
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_secret(token: str) -> str:
    raw = base64.urlsafe_b64decode(token.encode())
    return AESGCM(_enc_key()).decrypt(raw[:12], raw[12:], b"pfc-totp").decode()


# ---------- JWT ----------
@lru_cache
def _signing_key() -> Ed25519PrivateKey:
    pem = get_settings().jwt_private_key
    if pem is None:
        return Ed25519PrivateKey.generate()  # dev/test only (prod config requires it)
    key = serialization.load_pem_private_key(pem.get_secret_value().encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("JWT_PRIVATE_KEY must be an Ed25519 key")
    return key


def make_access_token(user_id: uuid.UUID, role: str, session_id: uuid.UUID, aud: Audience) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "aud": aud,
        "sub": str(user_id),
        "role": role,
        "sid": str(session_id),
        "iat": now,
        "exp": now + timedelta(seconds=get_settings().access_ttl_seconds),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(claims, _signing_key(), algorithm=ALGORITHM)


def read_access_token(token: str, aud: Audience) -> dict[str, Any] | None:
    try:
        return jwt.decode(
            token,
            _signing_key().public_key(),
            algorithms=[ALGORITHM],  # pinned: rejects "none"/HS256 confusion
            audience=aud,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "sid", "aud", "iss"]},
        )
    except jwt.PyJWTError:
        return None
