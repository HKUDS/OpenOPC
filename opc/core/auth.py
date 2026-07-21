"""Authentication and JWT gateway utilities for OpenOPC Shadow Mode."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

import secrets

# JWT secret key configuration; uses OPC_JWT_SECRET environment variable or generates secure runtime key
_env_secret = os.getenv("OPC_JWT_SECRET")
JWT_SECRET_KEY = _env_secret if _env_secret else secrets.token_hex(32)
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_SECONDS = 86400 * 7  # 7 days expiration for human contractors


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Hash a password using PBKDF2 HMAC SHA256 with a salt."""
    if salt is None:
        salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        100000
    )
    return f"{salt.hex()}${key.hex()}"


def verify_password(password: str, hashed_password: str) -> bool:
    """Verify a plain password against a stored PBKDF2 hash."""
    try:
        salt_hex, key_hex = hashed_password.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected_key = bytes.fromhex(key_hex)
        computed_key = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            100000
        )
        return hmac.compare_digest(computed_key, expected_key)
    except Exception:
        return False


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _base64url_decode(data_str: str) -> bytes:
    padding = "=" * (4 - (len(data_str) % 4))
    return base64.urlsafe_b64decode((data_str + padding).encode("utf-8"))


def create_jwt_token(payload: dict[str, Any], secret_key: str = JWT_SECRET_KEY, expires_in: int = JWT_EXPIRATION_SECONDS) -> str:
    """Generate a signed JWT token containing contractor identity & scope."""
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    
    token_payload = dict(payload)
    now = int(time.time())
    token_payload["iat"] = now
    token_payload["exp"] = now + expires_in
    payload_bytes = json.dumps(token_payload, separators=(",", ":")).encode("utf-8")

    unsigned_token = f"{_base64url_encode(header_bytes)}.{_base64url_encode(payload_bytes)}"
    signature = hmac.new(
        secret_key.encode("utf-8"),
        unsigned_token.encode("utf-8"),
        hashlib.sha256
    ).digest()
    
    return f"{unsigned_token}.{_base64url_encode(signature)}"


def verify_jwt_token(token: str, secret_key: str = JWT_SECRET_KEY) -> dict[str, Any] | None:
    """Validate a JWT token signature and expiration, returning claims if valid."""
    try:
        parts = token.strip().split(".")
        if len(parts) != 3:
            return None
        
        unsigned_token = f"{parts[0]}.{parts[1]}"
        expected_sig = hmac.new(
            secret_key.encode("utf-8"),
            unsigned_token.encode("utf-8"),
            hashlib.sha256
        ).digest()
        
        actual_sig = _base64url_decode(parts[2])
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        payload_json = _base64url_decode(parts[1]).decode("utf-8")
        payload = json.loads(payload_json)

        # Check expiration
        exp = payload.get("exp")
        if exp and int(time.time()) > int(exp):
            return None

        return payload
    except Exception:
        return None
