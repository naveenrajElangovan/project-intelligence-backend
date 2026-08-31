"""Pure Atlassian response validation and source mapping."""

from datetime import datetime
from typing import Any

from app.integrations.models import ProjectSource


def required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Atlassian response is missing {key}.")
    return value


def optional_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def scopes(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(scope for scope in value.split() if scope)
    if isinstance(value, list):
        return tuple(scope for scope in value if isinstance(scope, str) and scope)
    return ()


def authorization(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}


def jira_source(issue: dict[str, Any], project_id: str, resource_url: str) -> ProjectSource:
    key = required_string(issue, "key")
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    summary = str(fields.get("summary") or key)
    return ProjectSource(
        project_id=project_id,
        provider="JIRA",
        source_id=key,
        source_type="ISSUE",
        title=summary,
        reference=key,
        source_url=f"{resource_url.rstrip('/')}/browse/{key}",
        content=text_content(fields.get("description")),
        metadata={
            "issueType": nested_name(fields.get("issuetype")),
            "status": nested_name(fields.get("status")),
            "priority": nested_name(fields.get("priority")),
            "assignee": nested_display_name(fields.get("assignee")),
            "reporter": nested_display_name(fields.get("reporter")),
            "dueDate": fields.get("duedate"),
            "labels": fields.get("labels") or [],
        },
        source_updated_at=parse_datetime(fields.get("updated")),
    )


def confluence_page_source(
    page: dict[str, Any], project_id: str, resource_url: str
) -> ProjectSource:
    page_id = required_string(page, "id")
    title = str(page.get("title") or page_id)
    body = page.get("body") if isinstance(page.get("body"), dict) else {}
    storage = body.get("storage") if isinstance(body.get("storage"), dict) else {}
    links = page.get("_links") if isinstance(page.get("_links"), dict) else {}
    web_ui = links.get("webui")
    version = page.get("version") if isinstance(page.get("version"), dict) else {}
    return ProjectSource(
        project_id=project_id,
        provider="CONFLUENCE",
        source_id=f"page:{page_id}",
        source_type="PAGE",
        title=title,
        reference=page_id,
        source_url=(
            f"{resource_url.rstrip('/')}/wiki{web_ui}"
            if isinstance(web_ui, str) and web_ui
            else f"{resource_url.rstrip('/')}/wiki/pages/viewpage.action?pageId={page_id}"
        ),
        content=str(storage.get("value") or ""),
        metadata={"pageId": page_id, "status": page.get("status")},
        source_updated_at=parse_datetime(version.get("createdAt")),
    )


def confluence_attachment_source(
    attachment: dict[str, Any], page_id: str, project_id: str, resource_url: str
) -> ProjectSource:
    attachment_id = required_string(attachment, "id")
    title = str(attachment.get("title") or attachment_id)
    links = attachment.get("_links") if isinstance(attachment.get("_links"), dict) else {}
    metadata = attachment.get("metadata") if isinstance(attachment.get("metadata"), dict) else {}
    extensions = attachment.get("extensions") if isinstance(attachment.get("extensions"), dict) else {}
    version = attachment.get("version") if isinstance(attachment.get("version"), dict) else {}
    download = links.get("download")
    return ProjectSource(
        project_id=project_id,
        provider="CONFLUENCE",
        source_id=f"attachment:{attachment_id}",
        source_type="ATTACHMENT",
        title=title,
        reference=attachment_id,
        source_url=(
            f"{resource_url.rstrip('/')}/wiki{download}"
            if isinstance(download, str) and download
            else f"{resource_url.rstrip('/')}/wiki/pages/viewpageattachments.action?pageId={page_id}"
        ),
        content="",
        metadata={
            "attachmentId": attachment_id,
            "pageId": page_id,
            "mediaType": attachment.get("mediaType") or metadata.get("mediaType"),
            "fileSize": attachment.get("fileSize") or extensions.get("fileSize"),
            "comment": attachment.get("comment"),
        },
        source_updated_at=parse_datetime(version.get("createdAt")),
    )


def nested_name(value: object) -> str | None:
    return str(value.get("name")) if isinstance(value, dict) and value.get("name") else None


def nested_display_name(value: object) -> str | None:
    return str(value.get("displayName")) if isinstance(value, dict) and value.get("displayName") else None


def text_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text = value.get("text")
        children = value.get("content")
        parts = [str(text)] if isinstance(text, str) else []
        if isinstance(children, list):
            parts.extend(text_content(child) for child in children)
        return " ".join(part for part in parts if part).strip()
    if isinstance(value, list):
        return " ".join(text_content(item) for item in value).strip()
    return ""


def parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
