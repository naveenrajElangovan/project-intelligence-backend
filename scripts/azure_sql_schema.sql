/*
  Project Intelligence Azure SQL control-plane schema.

  Safe for an empty database: creates schema objects only. It inserts no projects,
  source content, chunks, embeddings, answers, or test records.
*/

SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF OBJECT_ID(N'dbo.projects', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.projects (
        project_id nvarchar(100) NOT NULL CONSTRAINT pk_projects PRIMARY KEY,
        display_name nvarchar(200) NOT NULL,
        active bit NOT NULL,
        jira_projects nvarchar(max) NOT NULL,
        confluence_spaces nvarchar(max) NOT NULL,
        github_repositories nvarchar(max) NOT NULL,
        vector_store nvarchar(max) NOT NULL,
        ingestion_schedule nvarchar(max) NOT NULL,
        created_at datetimeoffset NOT NULL,
        updated_at datetimeoffset NOT NULL,
        CONSTRAINT ck_projects_jira_json CHECK (ISJSON(jira_projects) = 1),
        CONSTRAINT ck_projects_confluence_json CHECK (ISJSON(confluence_spaces) = 1),
        CONSTRAINT ck_projects_github_json CHECK (ISJSON(github_repositories) = 1),
        CONSTRAINT ck_projects_vector_store_json CHECK (ISJSON(vector_store) = 1),
        CONSTRAINT ck_projects_schedule_json CHECK (ISJSON(ingestion_schedule) = 1)
    );
END;

IF OBJECT_ID(N'dbo.oauth_states', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.oauth_states (
        state_hash nvarchar(128) NOT NULL CONSTRAINT pk_oauth_states PRIMARY KEY,
        user_id nvarchar(100) NOT NULL,
        tenant_id nvarchar(100) NOT NULL,
        project_id nvarchar(100) NOT NULL,
        provider nvarchar(30) NOT NULL,
        expires_at datetimeoffset NOT NULL,
        user_email nvarchar(320) NULL,
        CONSTRAINT fk_oauth_states_projects FOREIGN KEY (project_id)
            REFERENCES dbo.projects(project_id)
    );
END;

IF OBJECT_ID(N'dbo.integration_connections', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.integration_connections (
        id int IDENTITY(1,1) NOT NULL CONSTRAINT pk_integration_connections PRIMARY KEY,
        project_id nvarchar(100) NOT NULL,
        provider nvarchar(30) NOT NULL,
        secret_reference nvarchar(500) NOT NULL,
        tenant_id nvarchar(100) NOT NULL,
        connected_by nvarchar(100) NOT NULL,
        resource_id nvarchar(200) NOT NULL,
        resource_url nvarchar(500) NOT NULL,
        resource_name nvarchar(200) NOT NULL,
        scopes nvarchar(max) NOT NULL,
        token_expires_at datetimeoffset NULL,
        connected_at datetimeoffset NOT NULL,
        updated_at datetimeoffset NOT NULL,
        last_synchronized_at datetimeoffset NULL,
        provider_account_id nvarchar(200) NULL,
        provider_display_name nvarchar(200) NULL,
        provider_email nvarchar(320) NULL,
        CONSTRAINT uq_integration_project_provider UNIQUE (project_id, provider),
        CONSTRAINT ck_integration_scopes_json CHECK (ISJSON(scopes) = 1),
        CONSTRAINT fk_integration_projects FOREIGN KEY (project_id)
            REFERENCES dbo.projects(project_id)
    );
END;

IF OBJECT_ID(N'dbo.alembic_version', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.alembic_version (
        version_num varchar(32) NOT NULL CONSTRAINT pk_alembic_version PRIMARY KEY
    );
END;

IF EXISTS (
    SELECT 1 FROM dbo.alembic_version WHERE version_num <> '20260816_0003'
)
BEGIN
    THROW 50001, 'Existing database must be upgraded with Alembic; bootstrap aborted.', 1;
END;

IF NOT EXISTS (SELECT 1 FROM dbo.alembic_version)
    INSERT INTO dbo.alembic_version (version_num) VALUES ('20260816_0003');

IF DATABASE_PRINCIPAL_ID(N'pi_backend_runtime') IS NULL
    EXEC(N'CREATE ROLE pi_backend_runtime');

GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.projects TO pi_backend_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.oauth_states TO pi_backend_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.integration_connections TO pi_backend_runtime;

COMMIT TRANSACTION;

SELECT name, type_desc
FROM sys.tables
WHERE name IN (
    N'projects',
    N'oauth_states',
    N'integration_connections',
    N'alembic_version'
)
ORDER BY name;
