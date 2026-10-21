"""CAS password encryption (AES-128-CBC + PKCS#7), matching the web login."""
from __future__ import annotations

import base64
import secrets

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

#: Character set used by the official ``encrypt.js`` for its random prefixes.
AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"


def random_string(length: int, alphabet: str = AES_CHARS) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


def encrypt_password(password: str, salt: str) -> str:
    """Encrypt a CAS password exactly like the site's ``encrypt.js``.

    ``key`` is the 16-byte page salt; the random IV is discarded (the server
    cannot use it because it is never transmitted).  The ciphertext's first
    block decrypts to the 64-character random prefix and is stripped by the
    server, which is why a normal CBC decrypt with a zero IV still works.
    """
    key = salt.encode("utf-8")
    if len(key) != 16:
        raise ValueError(f"CAS salt must be 16 bytes, got {len(key)}")
    iv = random_string(16).encode("utf-8")
    plaintext = (random_string(64) + password).encode("utf-8")

    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()

    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii")
