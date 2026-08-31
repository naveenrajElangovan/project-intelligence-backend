from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class EntraPrincipal:
    object_id: str
    subject: str
    tenant_id: str
    display_name: str
    username: str
    email: str | None
    claims: dict[str, Any]
