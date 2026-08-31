from datetime import UTC, datetime, timedelta

import httpx

from app.config import Settings
from app.integrations.models import AtlassianCredentials, ProjectSource
from app.integrations.atlassian.mappers import (
    authorization as _authorization,
    confluence_attachment_source as _confluence_attachment_source,
    confluence_page_source as _confluence_page_source,
    jira_source as _jira_source,
    optional_string as _optional_string,
    required_string as _required_string,
    scopes as _scopes,
)
from app.integrations.atlassian.models import AtlassianIdentity, AtlassianResource


class AtlassianOAuthClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http_client = http_client

    async def exchange_code(self, code: str) -> AtlassianCredentials:
        client = self._http_client or httpx.AsyncClient(timeout=15.0)
        close_client = self._http_client is None
        try:
            response = await client.post(
                "https://auth.atlassian.com/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "client_id": self._settings.atlassian_client_id,
                    "client_secret": self._settings.atlassian_client_secret,
                    "code": code,
                    "redirect_uri": self._settings.atlassian_redirect_uri,
                },
            )
            response.raise_for_status()
            payload = response.json()
            access_token = _required_string(payload, "access_token")
            expires_in = int(payload.get("expires_in", 3600))
            return AtlassianCredentials(
                access_token=access_token,
                refresh_token=_optional_string(payload, "refresh_token"),
                token_type=_optional_string(payload, "token_type") or "Bearer",
                expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
                scopes=_scopes(payload.get("scope")),
            )
        finally:
            if close_client:
                await client.aclose()

    async def accessible_resources(self, access_token: str) -> tuple[AtlassianResource, ...]:
        client = self._http_client or httpx.AsyncClient(timeout=15.0)
        close_client = self._http_client is None
        try:
            response = await client.get(
                "https://api.atlassian.com/oauth/token/accessible-resources",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("Atlassian returned an invalid accessible-resources response.")
            return tuple(
                AtlassianResource(
                    cloud_id=_required_string(item, "id"),
                    url=_required_string(item, "url"),
                    name=_required_string(item, "name"),
                    scopes=_scopes(item.get("scopes")),
                )
                for item in payload
                if isinstance(item, dict)
            )
        finally:
            if close_client:
                await client.aclose()

    async def current_user(self, access_token: str, cloud_id: str) -> AtlassianIdentity:
        client = self._http_client or httpx.AsyncClient(timeout=15.0)
        close_client = self._http_client is None
        try:
            response = await client.get(
                f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/myself",
                headers=_authorization(access_token),
            )
            response.raise_for_status()
            payload = response.json()
            return AtlassianIdentity(
                account_id=_required_string(payload, "accountId"),
                display_name=_required_string(payload, "displayName"),
                email=_optional_string(payload, "emailAddress"),
            )
        finally:
            if close_client:
                await client.aclose()

    async def refresh_credentials(self, refresh_token: str) -> AtlassianCredentials:
        client = self._http_client or httpx.AsyncClient(timeout=15.0)
        close_client = self._http_client is None
        try:
            response = await client.post(
                "https://auth.atlassian.com/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "client_id": self._settings.atlassian_client_id,
                    "client_secret": self._settings.atlassian_client_secret,
                    "refresh_token": refresh_token,
                },
            )
            response.raise_for_status()
            payload = response.json()
            expires_in = int(payload.get("expires_in", 3600))
            return AtlassianCredentials(
                access_token=_required_string(payload, "access_token"),
                refresh_token=_optional_string(payload, "refresh_token") or refresh_token,
                token_type=_optional_string(payload, "token_type") or "Bearer",
                expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
                scopes=_scopes(payload.get("scope")),
            )
        finally:
            if close_client:
                await client.aclose()

    async def jira_sources(
        self,
        access_token: str,
        cloud_id: str,
        project_id: str,
        project_key: str,
        resource_url: str,
    ) -> tuple[ProjectSource, ...]:
        client = self._http_client or httpx.AsyncClient(timeout=30.0)
        close_client = self._http_client is None
        try:
            url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/search/jql"
            params: dict[str, str | int] = {
                "jql": f'project = "{project_key}" ORDER BY updated DESC',
                "maxResults": 100,
                "fields": (
                    "summary,description,issuetype,status,priority,assignee,reporter,"
                    "created,updated,resolutiondate,duedate,labels,components,parent"
                ),
            }
            sources: list[ProjectSource] = []
            while True:
                response = await client.get(url, headers=_authorization(access_token), params=params)
                response.raise_for_status()
                payload = response.json()
                issues = payload.get("issues", [])
                if not isinstance(issues, list):
                    raise ValueError("Jira returned an invalid issue search response.")
                sources.extend(
                    _jira_source(issue, project_id, resource_url)
                    for issue in issues
                    if isinstance(issue, dict)
                )
                next_page_token = payload.get("nextPageToken")
                if not isinstance(next_page_token, str) or not next_page_token:
                    break
                params["nextPageToken"] = next_page_token
            return tuple(sources)
        finally:
            if close_client:
                await client.aclose()

    async def confluence_sources(
        self,
        access_token: str,
        cloud_id: str,
        project_id: str,
        space_id: str,
        resource_url: str,
    ) -> tuple[ProjectSource, ...]:
        client = self._http_client or httpx.AsyncClient(timeout=30.0)
        close_client = self._http_client is None
        try:
            origin = f"https://api.atlassian.com/ex/confluence/{cloud_id}"
            base = f"{origin}/wiki"
            url: str | None = f"{base}/api/v2/pages"
            params: dict[str, str | int] | None = {
                "space-id": space_id,
                "limit": 100,
                "body-format": "storage",
            }
            sources: list[ProjectSource] = []
            while url:
                response = await client.get(
                    url, headers=_authorization(access_token), params=params
                )
                response.raise_for_status()
                payload = response.json()
                results = payload.get("results", [])
                if not isinstance(results, list):
                    raise ValueError("Confluence returned an invalid pages response.")
                for page in results:
                    if not isinstance(page, dict):
                        continue
                    sources.append(
                        _confluence_page_source(
                            page, project_id, resource_url
                        )
                    )
                    page_id = _required_string(page, "id")
                    sources.extend(
                        await self._attachment_sources(
                            client,
                            access_token,
                            base,
                            page_id,
                            project_id,
                            resource_url,
                        )
                    )
                next_link = payload.get("_links", {}).get("next") if isinstance(payload.get("_links"), dict) else None
                if isinstance(next_link, str) and next_link:
                    url = next_link if next_link.startswith("http") else f"{origin}{next_link}"
                else:
                    url = None
                params = None
            return tuple(sources)
        finally:
            if close_client:
                await client.aclose()

    async def _attachment_sources(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        base: str,
        page_id: str,
        project_id: str,
        resource_url: str,
    ) -> tuple[ProjectSource, ...]:
        response = await client.get(
            f"{base}/api/v2/pages/{page_id}/attachments",
            headers=_authorization(access_token),
            params={"limit": 250},
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise ValueError("Confluence returned an invalid attachments response.")
        return tuple(
            _confluence_attachment_source(item, page_id, project_id, resource_url)
            for item in results
            if isinstance(item, dict)
        )


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Atlassian response is missing {key}.")
    return value


