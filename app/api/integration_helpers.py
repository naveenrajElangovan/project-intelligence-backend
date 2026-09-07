import html

from fastapi import HTTPException, status
from fastapi.responses import HTMLResponse

from app.auth.models import EntraPrincipal
from app.authorization.graph import GraphProjectAccessReader
from app.projects.models import ProjectDefinition
from app.projects.store import ProjectStore


def provider_secret_name(project_id: str, provider: str) -> str:
    safe_project = "".join(
        character.lower() if character.isalnum() else "-" for character in project_id
    ).strip("-")
    return f"pi-{safe_project}-{provider.lower()}-oauth"


def callback_page(title: str, message: str, success: bool) -> HTMLResponse:
    color = "#177245" if success else "#b42318"
    body = f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>{html.escape(title)}</title><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"></head><body style=\"font-family:system-ui;margin:40px;background:#0b1420;color:#fff\"><main style=\"max-width:620px;margin:auto;padding:32px;background:#132235;border-radius:20px\"><h1 style=\"color:{color}\">{html.escape(title)}</h1><p>{html.escape(message)}</p><p>You may close this browser window.</p></main></body></html>"""
    return HTMLResponse(body, status_code=200 if success else 400)


async def require_technical_lead_project(
    project_id: str,
    principal: EntraPrincipal,
    access_reader: GraphProjectAccessReader,
    project_store: ProjectStore,
) -> ProjectDefinition:
    context = await access_reader.read(principal.object_id)
    role = context.project_roles.get(project_id)
    if project_id not in context.projects or role is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "You do not have access to this project."
        )
    project = await project_store.get(project_id)
    if project is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "The project is not configured in Azure SQL."
        )
    if "TECHNICAL_LEAD" not in role:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the project technical lead can manage provider connections.",
        )
    return project
