"""CAS password encryption compatibility (AES-128-CBC, zero-IV server view)."""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.uestc.crypto import AES_CHARS, encrypt_password, random_string


def decrypt_like_server(token: str, salt: str) -> bytes:
    """The CAS server decrypts with a zero IV and ignores the first block."""
    ciphertext = base64.b64decode(token)
    decryptor = Cipher(
        algorithms.AES(salt.encode("utf-8")), modes.CBC(b"\x00" * 16)
    ).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return plain[16:]


def test_random_string_uses_expected_alphabet():
    value = random_string(128)
    assert len(value) == 128
    assert set(value) <= set(AES_CHARS)


def test_password_roundtrips_through_server_style_decryption():
    salt = "0123456789abcdef"
    password = "S3cret!密码"
    token = encrypt_password(password, salt)

    raw = base64.b64decode(token)
    assert len(raw) % 16 == 0
    recovered = decrypt_like_server(token, salt)
    # randomString(64) comes first; the password starts at byte 48 of the
    # post-first-block plaintext.
    assert recovered[48:].decode("utf-8") == password


def test_same_password_produces_different_ciphertexts():
    salt = "0123456789abcdef"
    first = encrypt_password("same-password", salt)
    second = encrypt_password("same-password", salt)
    assert first != second
    assert decrypt_like_server(first, salt)[48:].decode() == "same-password"
    assert decrypt_like_server(second, salt)[48:].decode() == "same-password"
