# Project Intelligence Backend

FastAPI control plane and authorized RAG gateway. It validates Microsoft Entra access tokens,
resolves project authorization, stores project/source mappings in Azure SQL, brokers Atlassian
credentials, and calls the private RAG service with server-derived access filters.

It does not ingest provider content and does not store source documents or embeddings. The
independent ingestion service writes authorized project content to Chroma. The independent RAG
service retrieves that content after backend authorization.

## Runtime relationship

```text
Frontend -> Backend -> RAG
                 |
Ingestion -------+  (project mappings and Atlassian gateway)
     |
     +-> Azure Table state
     +-> Chroma writes
```

The backend must be healthy before ingestion starts because ingestion reads project mappings and
Atlassian data through protected backend endpoints. RAG is not required to create embeddings, but
the development backend Compose configuration starts RAG because backend chat depends on it.

## Documentation

- [Enterprise security, privacy, and AI data architecture](PROJECT_INTELLIGENCE_SECURITY_ARCHITECTURE.md)
- [Current backend architecture](docs/CURRENT_ARCHITECTURE.md)
- [Current ingestion architecture](../project-intelligence-ingestion/docs/CURRENT_ARCHITECTURE.md)
- [Current RAG architecture](../project-intelligence-rag/docs/CURRENT_ARCHITECTURE.md)

## Configuration ownership

The backend `.env` contains backend runtime settings and secrets only. Project IDs, repositories,
branches, Jira project keys, Confluence spaces/root pages, Chroma routing, and schedules are rows
in the backend-owned SQL control plane, not environment variables. Local development currently uses
SQLite; production targets Azure SQL.

Development and production use the same environment key names. `.env.example` and
`.env.production.example` are the contract; only values and security policy change. Production
injects the same keys from the deployment platform and Azure Key Vault.

Paired service credentials must match:

| Caller | Backend key | Other service key |
|---|---|---|
| Ingestion -> backend | `PI_INGESTION_INTERNAL_API_KEY` | `PI_INGEST_CONTROL_PLANE_API_KEY` |
| Backend -> RAG | `PI_RAG_INTERNAL_API_KEY` | `PI_RAG_INTERNAL_API_KEY` |

Use independent random values for these two relationships. Never commit them.

# Development execution

## 1. Prerequisites

- Docker Desktop is running.
- Python 3.12+ and Azure CLI are installed.
- Azure CLI is signed in to the tenant that can access Azure SQL.
- The current Azure SQL schema/project rows exist.
- These sibling directories exist because Compose builds RAG from the sibling repository:

```text
<workspace>/project-intelligence-backend
<workspace>/project-intelligence-ingestion
<workspace>/project-intelligence-rag
```

## 2. Create the backend environment

```bash
cd <workspace>/project-intelligence-backend
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
test -f .env || cp .env.example .env
```

Update `.env` with the real development secrets and endpoints. Keep project mappings out of it.

The RAG dependency also needs its own file:

```bash
cd <workspace>/project-intelligence-rag
test -f .env || cp .env.example .env
```

Configure the same backend-to-RAG internal key in both files. Return to backend:

```bash
cd <workspace>/project-intelligence-backend
```

## 3. Authenticate Azure CLI

```bash
az login --tenant 11111111-1111-4111-8111-111111111111
az account show --output table
az account get-access-token \
  --resource https://database.windows.net/ \
  --query accessToken \
  --output tsv >/dev/null
```

Purpose: prove the current terminal can obtain an Azure SQL token. Do not paste or save that token.
`scripts/start_dev_api.sh` obtains a fresh token and passes it only to the backend container.

If using a separate Azure CLI cache, set `PI_DEV_SQL_AZURE_CONFIG_DIR` before running the script.

## 4. Run backend tests

```bash
cd <workspace>/project-intelligence-backend
.venv/bin/python -m pytest -q
```

## 5. Start RAG and backend

For the KMP application on macOS, use the unified local launcher. It validates the Azure CLI
identity, updates one dedicated Azure SQL firewall rule when the public IP changes, reuses or
starts MongoDB, starts native RAG, and starts the native backend with renewable SQL tokens:

```bash
cd <workspace>/project-intelligence-backend
./scripts/prepare_and_start_local_app.sh
```

The script exits successfully only after the backend `/ready` check passes. Start the KMP
application after it prints `Local services are ready`. Set `PI_DEV_SQL_FIREWALL_SYNC=false` to
disable the firewall update when connected through a VPN or private endpoint.

When switching from the Docker Backend to the native Backend, the launcher also restores the
existing encrypted provider credential file from the stopped Backend container if the native
`PI_LOCAL_SECRET_STORE_PATH` file is absent. The restored file remains encrypted and is written
with owner-only permissions. This keeps the Azure SQL `secret_reference` and the local Atlassian
credential store consistent without displaying credential contents.

For a complete Backend-plus-ingestion run, use the ingestion repository's single launcher instead
of starting components and checking them manually:

```bash
cd <workspace>/project-intelligence-ingestion
./scripts/run_unattended_ingestion.sh
```

That command invokes this Backend launcher, waits for `/ready`, and only then starts incremental
GitHub, Jira, and Confluence ingestion.

For the Dockerized development path instead:

```bash
cd <workspace>/project-intelligence-backend
./scripts/start_dev_api.sh
```

For Apple Silicon local inference, use
`PI_DEV_RAG_RUNTIME=native ./scripts/start_dev_api.sh`. This keeps the backend in Docker while
running the RAG process on macOS so the BGE reranker can use MPS. The default `docker` mode is for
an explicitly provisioned container GPU environment.

The script:

1. validates the Azure CLI session;
2. obtains a short-lived Azure SQL token;
3. builds/recreates the backend API container;
4. starts its `secrets-init` and `rag` Compose dependencies;
5. exposes backend on `127.0.0.1:8001` and RAG on `127.0.0.1:8003`.

For local Docker development, the script also loads the ingestion environment
file for Compose interpolation so ingestion and RAG use the same existing
Chroma project credential. The credential remains runtime-only and is not
copied into the backend repository or image. Production must inject the RAG
credential from its deployment secret store.

It does not start ingestion.

The Azure CLI step is a Docker Desktop development bridge only. Azure-hosted deployments should
attach the existing user-assigned managed identity `pi-backend-runtime` (client ID
`33333333-3333-4333-8333-333333333333`) and set
`PI_DATABASE_MANAGED_IDENTITY_CLIENT_ID` to that value. The identity already exists as the Azure
SQL external user `pi-backend-runtime` and belongs to the least-privilege `pi_backend_runtime`
database role. In that environment `PI_DATABASE_ACCESS_TOKEN` must remain empty and no interactive
login is required.

## 6. Verify backend before ingestion

```bash
curl --fail-with-body http://localhost:8001/health
curl --fail-with-body http://localhost:8003/health
docker compose ps
docker compose logs --tail=150 api rag
```

Expected backend health contains `"status":"ok"`. Fix backend/Azure SQL/secret-store errors before
starting ingestion.

To verify the protected ingestion mapping, load the paired key from the ingestion `.env` and call:

```bash
cd <workspace>/project-intelligence-ingestion
set -a
source .env
set +a
curl --fail-with-body --silent --show-error \
  http://localhost:8001/v1/internal/ingestion/projects/DEMO \
  -H "Authorization: Bearer $PI_INGEST_CONTROL_PLANE_API_KEY" \
  | python3 -m json.tool
```

The response must contain the active project, source mappings, Chroma routing, and an Atlassian
connection before an all-provider ingestion can succeed.

## 7. Start ingestion second

The recommended local path is the ingestion repository's single unattended launcher:

```bash
cd <workspace>/project-intelligence-ingestion
./scripts/run_unattended_ingestion.sh
```

It starts this Backend dependency chain when necessary and performs all readiness waiting before
ingestion. Use the separate Backend launcher and manual health commands only for diagnosis.

Recommended local arrangement:

- backend and RAG run natively through this repository's unified macOS launcher;
- ingestion runs from its own host `.venv`, allowing `DefaultAzureCredential` to use the host Azure
  CLI session for Azure Table;
- frontend calls only backend on port `8001`.

## 8. Logs and shutdown

```bash
cd <workspace>/project-intelligence-backend
docker compose logs -f api rag
```

Stop without deleting cloud data:

```bash
docker compose down
```

Do not add `-v`; the development provider-token volume would be removed.

# Production execution

Production uses managed identities, Key Vault, private service networking, Azure SQL, Azure Table,
and immutable images. A checked-in production `.env` is not used in Azure. The
`.env.production.example` files define the required key names for the deployment platform.

## 1. Production prerequisites

- Azure SQL schema migration completed as a controlled deployment step.
- Backend managed identity has the least-privilege Azure SQL database role and Key Vault access.
- RAG and ingestion identities have their service-specific permissions.
- Project/source/Chroma configuration exists in Azure SQL.
- Atlassian connection was bootstrapped once and its token secret is in Key Vault.
- Images are built, scanned, tagged immutably, and pushed to the registry.
- Backend public ingress uses HTTPS/WAF; RAG and ingestion control-plane traffic use private ingress.

## 2. Validate production configuration before deployment

On a secure deployment runner, populate the same environment key names shown in:

```text
project-intelligence-backend/.env.production.example
project-intelligence-rag/.env.production.example
project-intelligence-ingestion/.env.production.example
```

Required production differences include:

- `PI_ENVIRONMENT=production`, HTTPS on, explicit hosts/CORS, docs off;
- backend Azure SQL managed-identity client ID and `PI_DATABASE_ACCESS_TOKEN` empty;
- `PI_SECRET_STORE_BACKEND=azure-key-vault` with an HTTPS vault URL;
- production Entra/Atlassian settings;
- private RAG URL and workload credential;
- private backend control-plane URL for ingestion;
- ingestion Azure Table managed-identity client ID.

The current configuration validators intentionally fail startup when unsafe production values are
used.

## 3. Build immutable images

From the backend repository, which orchestrates the three build contexts:

```bash
cd <workspace>/project-intelligence-backend
export PI_IMAGE_TAG=<immutable-release-tag>
docker compose -f docker-compose.production.yml build rag api ingestion
```

Push the resulting images to the approved registry using the deployment pipeline. Do not use
`latest` for production.

## 4. Deploy in dependency order

Deploy/revise services in this order:

1. RAG private service; wait for its health/readiness checks.
2. Backend API; wait for health and verify Azure SQL, Key Vault, Entra metadata, and private RAG connectivity.
3. Ingestion webhook/manual-trigger service; verify `/health` and `/ready`.
4. Ingestion scheduled job and queue worker using the same ingestion image and configuration.
5. Frontend after the backend public endpoint is healthy.

Do not start a scheduled/full ingestion while backend readiness or the internal project endpoint is
failing.

For a single-host production-style Compose validation only, create the external network and start
the same order:

```bash
docker network inspect project-intelligence-edge >/dev/null 2>&1 || \
  docker network create project-intelligence-edge

PI_IMAGE_TAG="$PI_IMAGE_TAG" \
  docker compose -f docker-compose.production.yml up -d rag api

PI_IMAGE_TAG="$PI_IMAGE_TAG" \
  docker compose -f docker-compose.production.yml up -d ingestion

PI_IMAGE_TAG="$PI_IMAGE_TAG" \
  docker compose -f docker-compose.production.yml ps
```

This Compose path requires secure local `.env.production` files and a runtime capable of using the
configured identities. It is a validation path, not a replacement for Azure secret references and
managed networking.

## 5. Production verification

Verify through the configured ingress/probes:

- backend `/health` succeeds;
- RAG `/health` and `/ready` succeed privately;
- ingestion `/health` and `/ready` succeed;
- an authorized Entra user receives only assigned projects from `/v1/me`;
- `/v1/projects/{projectId}/access-context` combines the Entra assignment with backend-owned
  Jira, Confluence, and GitHub readiness before the client enables questions;
- an unauthorized project request returns `403`;
- the internal ingestion project response is available only to the ingestion workload;
- backend-to-RAG retrieval applies project and access-policy filters;
- no secrets, tokens, source bodies, or questions appear in logs.

## 6. Start production ingestion

After backend and ingestion readiness:

1. run one explicit full bootstrap/reconciliation for a newly configured project;
2. run normal incremental ingestion daily;
3. keep GitHub merged-PR webhook delivery enabled;
4. use provider-specific runs only for diagnosis/recovery.

Exact job commands and verification are in the
[ingestion README](../project-intelligence-ingestion/README.md#production-execution).

## 7. Rollback and shutdown

- Roll back backend/RAG/ingestion to the previous immutable image tag.
- Do not roll back Azure SQL schema blindly; use a reviewed migration/restore plan.
- Preserve Azure Table manifests and Chroma collections unless a documented data rollback requires
  switching namespaces.
- Stop scheduled jobs before taking backend control-plane access offline.

## Atlassian MCP and Forge events

The development Compose stack includes the separate
`project-intelligence-atlassian` service on `127.0.0.1:8005`. The backend remains
authoritative for project mappings and encrypted OAuth records and registers
ephemeral, project-qualified user sessions with that service. Jira and
Confluence content is never returned through the backend's public API.

The shared design, configuration, recovery behavior, read-only controls, and
rollout checks are documented in the RAG repository at
`docs/ATLASSIAN_MCP_AND_EVENTS.md`.
