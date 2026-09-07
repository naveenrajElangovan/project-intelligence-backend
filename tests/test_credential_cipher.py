import base64

import pytest

from app.integrations.crypto import CredentialCipher


def _key(value: bytes) -> str:
    return base64.b64encode(value).decode()


def test_decrypt_rejects_credentials_encrypted_with_another_key() -> None:
    encrypted = CredentialCipher(_key(b"a" * 32)).encrypt({"accessToken": "secret"})

    with pytest.raises(ValueError, match="cannot be decrypted"):
        CredentialCipher(_key(b"b" * 32)).decrypt(encrypted)


def test_decrypt_rejects_malformed_ciphertext() -> None:
    with pytest.raises(ValueError, match="cannot be decrypted"):
        CredentialCipher(_key(b"a" * 32)).decrypt("not-valid-ciphertext")
