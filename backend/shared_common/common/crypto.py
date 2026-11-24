"""Encryption of chat text at rest.

Text is encrypted with Fernet (AES-128-CBC + HMAC-SHA256, from the
`cryptography` package, free and open source). Encrypted values look like

    enc:v1:<token>

so old plain-text rows can still be read (no prefix = not encrypted yet) and
`encrypt_existing.py` can tell which rows still need encrypting.

Keys
----
CHAT_ENCRYPTION_KEYS holds one or more Fernet keys, separated by commas.
The FIRST key encrypts new data. Every key can decrypt, which is how you
rotate: put a new key in front, keep the old one behind it, run
`python encrypt_existing.py --rekey`, then drop the old key.

Make a key:   python crypto.py genkey

Lose the keys and the stored text is gone for good. Back them up.
"""

import os
import sys

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from dotenv import load_dotenv
from sqlalchemy.types import Text, TypeDecorator

load_dotenv()

PREFIX = "enc:v1:"

_fernet: MultiFernet | None = None


class EncryptionConfigError(RuntimeError):
    """CHAT_ENCRYPTION_KEYS is missing or not valid."""


def _keys() -> MultiFernet:
    """Build the cipher on first use, so importing this file never fails."""

    global _fernet

    if _fernet is not None:
        return _fernet

    raw = os.getenv("CHAT_ENCRYPTION_KEYS", "")
    keys = [part.strip() for part in raw.split(",") if part.strip()]

    if not keys or keys == ["your_encryption_keys_here"]:
        raise EncryptionConfigError(
            "CHAT_ENCRYPTION_KEYS is not set. Make one with: python crypto.py genkey"
        )

    try:
        _fernet = MultiFernet([Fernet(key.encode()) for key in keys])
    except (ValueError, TypeError) as error:
        raise EncryptionConfigError(
            "CHAT_ENCRYPTION_KEYS holds a key that is not a valid Fernet key."
        ) from error

    return _fernet


def check_configured() -> None:
    """Call at start up so a missing key stops the service right away,
    not on the first message."""

    _keys()


def is_encrypted(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(plain: str) -> str:
    """Always encrypts, even if the text happens to start with the prefix."""

    return PREFIX + _keys().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    """Plain text passes through unchanged (rows from before encryption)."""

    if not is_encrypted(value):
        return value

    try:
        return _keys().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as error:
        raise EncryptionConfigError(
            "Could not decrypt a stored value: wrong or missing key in "
            "CHAT_ENCRYPTION_KEYS."
        ) from error


def rekey(value: str) -> str:
    """Re-encrypt with the newest key (used when rotating keys)."""

    return PREFIX + _keys().rotate(value[len(PREFIX):].encode("ascii")).decode("ascii")


class EncryptedText(TypeDecorator):
    """A text column that is encrypted on the way in and decrypted on the way out."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else encrypt(value)

    def process_result_value(self, value, dialect):
        return None if value is None else decrypt(value)


if __name__ == "__main__":
    if sys.argv[1:] == ["genkey"]:
        print(Fernet.generate_key().decode())
    else:
        sys.exit("Usage: python crypto.py genkey")
