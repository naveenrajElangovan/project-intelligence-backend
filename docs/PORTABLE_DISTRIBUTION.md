# Portable single-file distribution

Build one encrypted, self-extracting application bundle from the current development stack:

```bash
cd /Users/naveenrajelangovan/Desktop/project-intelligence-backend
./scripts/build_portable_bundle.sh
```

The builder prompts twice for a password of at least 16 characters. It stops the data-writing
services, copies consistent ChromaDB, MongoDB, and provider-secret volume snapshots, and restarts
the local stack automatically. The output is:

```text
/Users/naveenrajelangovan/Desktop/project-intelligence-portable.run
```

Transfer that single file through an encrypted channel. Send its password through a different
channel. The recipient needs Docker Desktop, Docker Compose v2, Bash, OpenSSL, internet access for
the first native image build, at least 15 GB of free disk, and preferably 16 GB or more of RAM.

The recipient runs one command:

```bash
bash project-intelligence-portable.run
```

The installer detects ARM64 or AMD64, builds the unchanged service code for that architecture,
restores the captured databases and vectors, starts all services, and waits for health checks.
It refuses to overwrite an existing Project Intelligence Docker volume.

The bundle includes sensitive `.env` files, OAuth material, conversations, manifests, and the
provider-secret store only inside its encrypted payload. Do not publish the `.run` file or commit
it to Git. The native Compose Desktop client is distributed separately for each operating system;
the portable file provides the complete backend, RAG, ingestion, Atlassian, MongoDB, and ChromaDB
runtime at `http://127.0.0.1:8001`.
