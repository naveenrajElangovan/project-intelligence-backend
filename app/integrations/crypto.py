import base64
import binascii
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialCipher:
    def __init__(self, encoded_key: str) -> None:
        try:
            key = base64.b64decode(encoded_key, validate=True)
        except ValueError as error:
            raise ValueError("PI_PROVIDER_TOKEN_ENCRYPTION_KEY must be valid Base64.") from error
        if len(key) != 32:
            raise ValueError("PI_PROVIDER_TOKEN_ENCRYPTION_KEY must decode to exactly 32 bytes.")
        self._cipher = AESGCM(key)

    def encrypt(self, value: dict[str, Any]) -> str:
        nonce = os.urandom(12)
        plaintext = json.dumps(value, separators=(",", ":")).encode()
        ciphertext = self._cipher.encrypt(nonce, plaintext, None)
        return base64.urlsafe_b64encode(nonce + ciphertext).decode()

    def decrypt(self, value: str) -> dict[str, Any]:
        try:
            payload = base64.urlsafe_b64decode(value.encode())
            if len(payload) <= 12:
                raise ValueError("Encrypted provider credentials are malformed.")
            plaintext = self._cipher.decrypt(payload[:12], payload[12:], None)
            decoded = json.loads(plaintext)
        except (binascii.Error, InvalidTag, UnicodeDecodeError, ValueError) as error:
            raise ValueError(
                "Encrypted provider credentials cannot be decrypted with the configured key."
            ) from error
        if not isinstance(decoded, dict):
            raise ValueError("Encrypted provider credentials are malformed.")
        return decoded
