"""Password-at-rest encryption and signed admin session cookies."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SESSION_COOKIE = "uestc_checkin_session"
DEFAULT_SESSION_TTL = 7 * 24 * 3600


def load_or_create_secret(data_dir: Path) -> str:
    """Return a persistent app secret, creating ``data/secret.key`` if needed.

    Keeping the secret in the data volume means passwords survive container
    recreation even when ``APP_SECRET`` is not set.  Changing it later makes
    already-stored passwords undecryptable, so the UI warns about this.
    """
    path = Path(data_dir) / "secret.key"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_hex(32)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


class SecretBox:
    """Symmetric encryption for the CAS password, keyed by the app secret."""

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("app secret must not be empty")
        key = base64.urlsafe_b64encode(
            hashlib.sha256(secret.encode("utf-8") + b"|uestc-checkin-fernet").digest()
        )
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
            raise SecretMismatch(
                "无法解密已保存的密码：APP_SECRET 与数据库不匹配。"
                "请恢复原来的 APP_SECRET 或 data/secret.key。"
            ) from exc


class SecretMismatch(Exception):
    """Raised when stored ciphertext cannot be decrypted with the current key."""


def check_admin_password(configured: str, provided: str) -> bool:
    return hmac.compare_digest(
        (configured or "").encode("utf-8"), (provided or "").encode("utf-8")
    )


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_session(secret: str, payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    body = _b64e(raw.encode("utf-8"))
    signature = hmac.new(
        secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{body}.{_b64e(signature)}"


def verify_session(secret: str, token: str | None, now: int | None = None) -> dict | None:
    if not token or "." not in token:
        return None
    try:
        body, signature = token.rsplit(".", 1)
        expected = hmac.new(
            secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64d(signature), expected):
            return None
        payload = json.loads(_b64d(body).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        if int(payload.get("exp", 0)) < int(now if now is not None else time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def new_session(
    secret: str, ttl: int = DEFAULT_SESSION_TTL, now: int | None = None
) -> tuple[str, str]:
    """Create a signed session token plus its CSRF token."""
    issued = int(now if now is not None else time.time())
    csrf = secrets.token_urlsafe(24)
    payload = {
        "sub": "admin",
        "iat": issued,
        "exp": issued + int(ttl),
        "csrf": csrf,
    }
    return sign_session(secret, payload), csrf
