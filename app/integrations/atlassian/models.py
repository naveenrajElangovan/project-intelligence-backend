"""Atlassian transport value objects."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AtlassianResource:
    cloud_id: str
    url: str
    name: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AtlassianIdentity:
    account_id: str
    display_name: str
    email: str | None
