import asyncio
import json
import os
from pathlib import Path

from app.integrations.crypto import CredentialCipher


class LocalEncryptedSecretStore:
    """Encrypted development-only token store; never selected in production."""

    def __init__(self, path: str, encoded_key: str) -> None:
        self._path = Path(path)
        self._cipher = CredentialCipher(encoded_key)
        self._lock = asyncio.Lock()

    async def put_json(self, name: str, value: dict[str, object]) -> str:
        async with self._lock:
            values = await asyncio.to_thread(self._read)
            values[name] = self._cipher.encrypt(value)
            await asyncio.to_thread(self._write, values)
        return f"local://{name}"

    async def get_json(self, reference: str) -> dict[str, object]:
        name = reference.removeprefix("local://")
        values = await asyncio.to_thread(self._read)
        encrypted = values.get(name)
        if not isinstance(encrypted, str):
            raise KeyError("Provider secret does not exist.")
        return self._cipher.decrypt(encrypted)

    async def delete(self, reference: str) -> None:
        name = reference.removeprefix("local://")
        async with self._lock:
            values = await asyncio.to_thread(self._read)
            values.pop(name, None)
            await asyncio.to_thread(self._write, values)

    def _read(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        decoded = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("Local provider secret store is malformed.")
        return {str(key): str(value) for key, value in decoded.items()}

    def _write(self, values: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(values), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self._path)
