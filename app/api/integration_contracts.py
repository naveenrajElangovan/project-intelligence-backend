"""Public HTTP contracts for project integration endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class IntegrationStatusResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider: str
    configured: bool
    connected: bool
    available: bool
    resource_name: str | None = Field(default=None, alias="resourceName")
    resource_url: str | None = Field(default=None, alias="resourceUrl")
    connected_at: datetime | None = Field(default=None, alias="connectedAt")
    last_synchronized_at: datetime | None = Field(default=None, alias="lastSynchronizedAt")
    message: str


class ConnectResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider: str
    authorization_url: str = Field(alias="authorizationUrl")
    expires_at: datetime = Field(alias="expiresAt")


class SynchronizationResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(alias="projectId")
    synchronized_at: datetime = Field(alias="synchronizedAt")
    jira_issues: int = Field(alias="jiraIssues")
    confluence_pages: int = Field(alias="confluencePages")
    confluence_attachments: int = Field(alias="confluenceAttachments")


class ProjectSourceResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provider: str
    source_id: str = Field(alias="sourceId")
    source_type: str = Field(alias="sourceType")
    title: str
    reference: str
    source_url: str = Field(alias="sourceUrl")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")


class ProjectAccessContextResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(alias="projectId")
    display_name: str = Field(alias="displayName")
    role: str
    authorized: bool
    authorization_mode: str = Field(alias="authorizationMode")
    can_ask_questions: bool = Field(alias="canAskQuestions")
    integrations: list[IntegrationStatusResponse]
