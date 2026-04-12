"""
GrooveIQ – Credential encryption helpers.

Uses Fernet (AES-128-CBC + HMAC-SHA256) to encrypt/decrypt credentials
at rest.  When ``CREDENTIAL_ENCRYPTION_KEY`` is configured, media server
passwords and tokens are stored encrypted in the environment / .env file
and decrypted on demand.

When no encryption key is configured, credentials are used as-is
(plaintext) for backwards compatibility.
"""

from __future__ import annotations

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


def _get_fernet():
    """Return a Fernet instance or None if encryption is not configured."""
    key = settings.CREDENTIAL_ENCRYPTION_KEY
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key.encode() if isinstance(key, str) else key)


def decrypt_credential(value: str) -> str:
    """Decrypt a Fernet-encrypted credential, or return as-is if not encrypted.

    Fernet tokens always start with ``gAAAAA`` (base64-encoded version byte).
    If the value doesn't look like a Fernet token, it's treated as plaintext.
    """
    if not value:
        return value
    f = _get_fernet()
    if f is None:
        return value
    # Heuristic: Fernet tokens are base64 and start with "gAAAAA"
    if not value.startswith("gAAAAA"):
        logger.warning(
            "CREDENTIAL_ENCRYPTION_KEY is set but credential does not look "
            "Fernet-encrypted. Using as plaintext. Encrypt it with: "
            "python -m app.core.keygen --encrypt (inside the container)"
        )
        return value
    try:
        return f.decrypt(value.encode("ascii")).decode("utf-8")
    except Exception:
        logger.error("Failed to decrypt credential. Check CREDENTIAL_ENCRYPTION_KEY.")
        raise


def encrypt_credential(plaintext: str) -> str:
    """Encrypt a plaintext credential with the configured Fernet key."""
    f = _get_fernet()
    if f is None:
        raise ValueError("CREDENTIAL_ENCRYPTION_KEY is not configured. Generate one with: openssl rand -base64 32")
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def get_media_server_password() -> str:
    """Return the decrypted Navidrome password."""
    return decrypt_credential(settings.MEDIA_SERVER_PASSWORD)


def get_media_server_token() -> str:
    """Return the decrypted Plex token."""
    return decrypt_credential(settings.MEDIA_SERVER_TOKEN)
