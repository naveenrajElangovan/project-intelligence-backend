import asyncio
import json

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient


class AzureKeyVaultSecretStore:
    """Stores provider credentials in Key Vault using workload managed identity."""

    def __init__(self, vault_url: str) -> None:
        credential = DefaultAzureCredential(
            exclude_interactive_browser_credential=True,
            exclude_broker_credential=True,
        )
        self._client = SecretClient(vault_url=vault_url, credential=credential)

    async def put_json(self, name: str, value: dict[str, object]) -> str:
        encoded = json.dumps(value, separators=(",", ":"))
        secret = await asyncio.to_thread(self._client.set_secret, name, encoded)
        return secret.id

    async def get_json(self, reference: str) -> dict[str, object]:
        name, version = _secret_identity(reference)
        secret = await asyncio.to_thread(self._client.get_secret, name, version)
        decoded = json.loads(secret.value)
        if not isinstance(decoded, dict):
            raise ValueError("Key Vault provider secret is malformed.")
        return decoded

    async def delete(self, reference: str) -> None:
        name, _ = _secret_identity(reference)
        poller = await asyncio.to_thread(self._client.begin_delete_secret, name)
        await asyncio.to_thread(poller.wait)


def _secret_identity(reference: str) -> tuple[str, str | None]:
    parts = reference.rstrip("/").split("/")
    if len(parts) < 2 or parts[-2] == "secrets":
        return parts[-1], None
    return parts[-2], parts[-1]
