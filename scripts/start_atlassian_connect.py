"""Begin the Atlassian OAuth flow from the command line and print the consent URL.

Why this exists. The Atlassian gateway that ingestion needs -- cloudId and
resourceUrl -- is derived entirely from the `integration_connections` row (see
_atlassian_metadata in app/api/internal_ingestion.py). A control plane rebuilt
from scratch has no such row, so Confluence and Jira ingestion fails with "The
backend has no Atlassian gateway for this project", and the desktop client has no
connect-integration screen to create one.

The HTTP endpoint that starts this flow requires an Entra bearer token, which is
awkward to obtain outside the app. This script performs the same two steps the
endpoint performs -- persist a hashed one-time state, build the consent URL --
against the same store, so the HTTP surface keeps its authorization intact and
nothing here is a bypass of the callback's own checks: the callback still
verifies the state, still enforces email matching, and still encrypts the tokens.

Development only, and it refuses to run in production.

    .venv/bin/python -m scripts.start_atlassian_connect --project DEMO \
        --user-object-id <entra oid> --tenant-id <entra tid> --email you@example.com
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import secrets
from urllib.parse import urlencode

from app.config import get_settings
from app.integrations.dependencies import get_integration_store
from app.integrations.models import OAuthState
from app.projects.dependencies import get_project_store


STATE_MINUTES = 10


async def _run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--user-object-id",
        required=True,
        help="Entra object id (oid) of the signing-in user. It is recorded on the "
        "connection and checked by the callback, so it must be the account that "
        "will approve consent.",
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--email", default="")
    arguments = parser.parse_args()

    settings = get_settings()
    if settings.is_production:
        raise SystemExit("Refusing to start an OAuth flow this way in production.")
    if not settings.atlassian_oauth_configured:
        raise SystemExit(
            "Atlassian OAuth is not configured: set PI_ATLASSIAN_CLIENT_ID, "
            "PI_ATLASSIAN_CLIENT_SECRET and PI_ATLASSIAN_REDIRECT_URI."
        )

    project = await get_project_store().get(arguments.project)
    if project is None:
        raise SystemExit(f"Project {arguments.project} is not in the control plane.")
    if not project.jira_projects and not project.confluence_spaces:
        raise SystemExit(
            f"Project {arguments.project} has no Jira or Confluence sources, so a "
            "connection would have nothing to authorize. Seed the mapping first."
        )

    raw_state = secrets.token_urlsafe(48)
    expires_at = datetime.now(UTC) + timedelta(minutes=STATE_MINUTES)
    await get_integration_store().save_oauth_state(
        OAuthState(
            # Only the hash is stored, so a leaked control plane cannot be used
            # to complete someone else's pending flow.
            state_hash=hashlib.sha256(raw_state.encode()).hexdigest(),
            user_id=arguments.user_object_id,
            tenant_id=arguments.tenant_id,
            project_id=arguments.project,
            provider="ATLASSIAN",
            expires_at=expires_at,
            user_email=arguments.email or None,
        )
    )
    query = urlencode(
        {
            "audience": "api.atlassian.com",
            "client_id": settings.atlassian_client_id,
            "scope": settings.atlassian_scopes,
            "redirect_uri": settings.atlassian_redirect_uri,
            "state": raw_state,
            "response_type": "code",
            "prompt": "consent",
        }
    )
    print("Open this URL in a browser and approve the Atlassian consent screen.")
    print(f"It expires at {expires_at.isoformat()} ({STATE_MINUTES} minutes).")
    print()
    print(f"https://auth.atlassian.com/authorize?{query}")
    print()
    print(
        "The redirect lands on "
        f"{settings.atlassian_redirect_uri}, which stores the connection. "
        "Then re-run the Confluence ingestion."
    )


if __name__ == "__main__":
    asyncio.run(_run())
