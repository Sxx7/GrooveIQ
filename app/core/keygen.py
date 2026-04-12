"""
Generate cryptographically secure API keys and encrypt credentials for GrooveIQ.

Usage:
    python -m app.core.keygen              # generate 1 API key
    python -m app.core.keygen 3            # generate 3 API keys
    python -m app.core.keygen --fernet     # generate a Fernet encryption key
    python -m app.core.keygen --encrypt    # encrypt a credential (reads from stdin)
"""

from __future__ import annotations

import secrets
import sys


def generate_api_key() -> str:
    """Return a 43-character URL-safe token (256 bits of entropy)."""
    return secrets.token_urlsafe(32)


def generate_fernet_key() -> str:
    """Return a Fernet encryption key."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def encrypt_value(plaintext: str, fernet_key: str) -> str:
    """Encrypt a plaintext value with a Fernet key."""
    from cryptography.fernet import Fernet

    f = Fernet(fernet_key.encode())
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def main() -> None:
    args = sys.argv[1:]

    if "--fernet" in args:
        print(generate_fernet_key())
        return

    if "--encrypt" in args:
        import getpass

        key = input("Fernet key (CREDENTIAL_ENCRYPTION_KEY): ").strip()
        if not key:
            print("Error: Fernet key is required.", file=sys.stderr)
            sys.exit(1)
        plaintext = getpass.getpass("Credential to encrypt: ")
        if not plaintext:
            print("Error: empty credential.", file=sys.stderr)
            sys.exit(1)
        print(encrypt_value(plaintext, key))
        return

    count = 1
    for a in args:
        if a.isdigit():
            count = int(a)
    for _ in range(count):
        print(generate_api_key())


if __name__ == "__main__":
    main()
