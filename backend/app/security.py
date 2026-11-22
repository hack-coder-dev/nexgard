from __future__ import annotations

import os
from cryptography.fernet import Fernet, InvalidToken


class CredentialVault:
    """Encrypts device passwords before they are stored in SQLite."""

    def __init__(self) -> None:
        key = os.getenv("NOVATECH_ENCRYPTION_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "NOVATECH_ENCRYPTION_KEY is missing. Run ./setup.sh to create the local .env file."
            )
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except Exception as exc:
            raise RuntimeError("NOVATECH_ENCRYPTION_KEY is invalid.") from exc

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise RuntimeError("A saved device credential could not be decrypted.") from exc
