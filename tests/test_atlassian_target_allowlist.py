"""The Atlassian proxy allowlist is a security boundary; widening it needs tests.

A Confluence attachment's `downloadLink` is a v1 REST content path, which no
prefix in the original list covered, so every page with an attachment failed the
whole ingestion run with a 422.
"""

import pytest
from fastapi import HTTPException

from app.api.internal_ingestion import _validate_atlassian_target


CLOUD = "66666666-6666-4666-8666-666666666666"
BASE = f"https://api.atlassian.com/ex/confluence/{CLOUD}"
JIRA = f"https://api.atlassian.com/ex/jira/{CLOUD}"


def _allowed(target: str) -> bool:
    try:
        _validate_atlassian_target(target, CLOUD)
    except HTTPException:
        return False
    return True


@pytest.mark.parametrize(
    "target",
    [
        f"{BASE}/wiki/rest/api/content/7995393/child/attachment/att7045122/download",
        f"{BASE}/wiki/rest/api/content/1/child/attachment/att_1.2-3/download",
        f"{BASE}/wiki/download/attachments/7995393/spec.pdf",
        f"{BASE}/download/attachments/7995393/spec.pdf",
        f"{BASE}/wiki/api/v2/pages",
        f"{BASE}/wiki/api/v2/pages/7995393/attachments",
        f"{BASE}/wiki/rest/api/content/search?cql=x",
        f"{JIRA}/rest/api/3/search/jql",
        f"{JIRA}/rest/api/3/attachment/content/10001",
    ],
)
def test_permitted_targets(target: str) -> None:
    assert _allowed(target)


@pytest.mark.parametrize(
    "target",
    [
        # Nothing else under the Confluence content API may be reached.
        f"{BASE}/wiki/rest/api/user/current",
        f"{BASE}/wiki/rest/api/content/7995393",
        f"{BASE}/wiki/rest/api/space",
        # The pattern is anchored at both ends, so no suffix can be appended.
        f"{BASE}/wiki/rest/api/content/1/child/attachment/att1/download/../../admin",
        f"{BASE}/wiki/rest/api/content/1/child/attachment/att1/downloadx",
        # A non-numeric content id is not a content id.
        f"{BASE}/wiki/rest/api/content/abc/child/attachment/att1/download",
        # Another tenant's cloud id must never resolve.
        "https://api.atlassian.com/ex/confluence/00000000-0000-0000-0000-000000000000"
        "/wiki/rest/api/content/1/child/attachment/att1/download",
        # Only Atlassian, only HTTPS.
        f"http://api.atlassian.com/ex/confluence/{CLOUD}/wiki/api/v2/pages",
        f"https://evil.example.com/ex/confluence/{CLOUD}/wiki/api/v2/pages",
    ],
)
def test_rejected_targets(target: str) -> None:
    assert not _allowed(target)
