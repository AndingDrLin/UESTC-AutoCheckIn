"""Password-at-rest encryption and signed admin sessions."""
from __future__ import annotations

import pytest

from app.security import (
    SecretBox,
    SecretMismatch,
    check_admin_password,
    load_or_create_secret,
    new_session,
    verify_session,
)


def test_secret_is_created_once_and_reused(tmp_path):
    first = load_or_create_secret(tmp_path)
    second = load_or_create_secret(tmp_path)
    assert first == second
    assert (tmp_path / "secret.key").read_text(encoding="utf-8").strip() == first


def test_secret_box_roundtrip_and_wrong_key(tmp_path):
    secret = load_or_create_secret(tmp_path)
    box = SecretBox(secret)
    token = box.encrypt("p@ssw0rd")
    assert token != "p@ssw0rd"
    assert box.decrypt(token) == "p@ssw0rd"

    with pytest.raises(SecretMismatch):
        SecretBox("a-different-secret").decrypt(token)


def test_admin_password_comparison():
    assert check_admin_password("hunter2", "hunter2") is True
    assert check_admin_password("hunter2", "hunter3") is False
    assert check_admin_password("", "") is True


def test_session_sign_verify_expiry_and_tampering():
    token, csrf = new_session("secret", ttl=60, now=1_000)
    payload = verify_session("secret", token, now=1_001)
    assert payload is not None
    assert payload["sub"] == "admin"
    assert payload["csrf"] == csrf

    # exp == 1060 is still accepted; 1061 is not.
    assert verify_session("secret", token, now=1_060) is not None
    assert verify_session("secret", token, now=1_061) is None

    assert verify_session("wrong-secret", token, now=1_001) is None
    assert verify_session("secret", "not-a-token", now=1_001) is None

    head, signature = token.split(".")
    # Flip the first signature character: this always changes the first decoded
    # byte, unlike the final base64 character whose low bits may be padding.
    tampered = head + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
    assert verify_session("secret", tampered, now=1_001) is None
