# Project Intelligence Backend — quality status

Last verified: 2026-09-16. This is the sole current quality and repository-hygiene
summary. `CURRENT_ARCHITECTURE.md` remains the backend architecture source of
truth. The root security architecture is a cross-system security reference;
`DEVELOPMENT.md` and `PORTABLE_DISTRIBUTION.md` are operating guides.

This repository follows the suite's canonical
[exact-pin dependency policy](https://github.com/naveenrajElangovan/project-intelligence-rag/blob/main/docs/DEPENDENCY_POLICY.md).

## Verified status

- Full suite: 185 passed, 1 third-party deprecation warning.
- Coverage: 75.00%; required floor: 73.35%.
- Ruff lint: clean across application, tests, scripts, and migrations.
- Ruff format: all 112 maintained Python files clean.
- Import-linter: two contracts kept, zero broken.
- Repository-wide mypy: 0 errors across 112 application, test, script, and
  migration files.
- Deptry: 0 unexplained dependency findings across the maintained tree.
- Dependency integrity: `pip check` reported no broken requirements.
- Tests contain no permanent `skip`, `pytest.skip`, `unittest.skip`, or `xfail`
  markers.

## Closed audit decisions

- **Integrations route size and formatting — closed:** OAuth token helpers and
  integration-status mapping now live in cohesive `integration_oauth.py` and
  `integration_status.py` modules. `integrations.py` is Ruff-formatted and 416
  lines, below the 550-line architecture guard.
- **Mypy debt — closed:** the ratchet was deleted after repository-wide mypy
  reached zero. The only missing-stub exceptions are module-specific overrides:
  Azure Service Bus 7.14.2 has no `py.typed` marker under mypy 1.20.2/Python
  3.13, and pyodbc 5.3.0 likewise publishes no typing metadata. No source-line
  `type: ignore` was added for this work.
- **Dependency duplication — closed:** `pyproject.toml` is the sole declaration;
  all runtime and development dependencies are exact pins, the duplicate
  `requirements.txt` was removed after a five-repository reference search, and
  Docker installs the project metadata directly.
- **Environment templates — closed:** both checked-in templates are intentional.
  `.env.example` is the complete local-development contract and is safe to copy
  to the ignored `.env`; `.env.production.example` is the deployment contract
  with production-only security and identity expectations. They serve distinct
  audiences and are not duplicate sources of defaults.
- **Unused dependencies — closed:** deptry is pinned and enforced by Make,
  pre-commit, and CI. Its narrow `DEP002` list contains only runtime-discovered
  drivers (`aioodbc`, `aiosqlite`), the `uvicorn` server entrypoint, and CLI-only
  quality tools (`deptry`, `import-linter`, `mypy`, `pre-commit`, `pytest-cov`,
  `ruff`). `DEP004=pytest` records that tests are scanned while pytest correctly
  remains development-only.
- **Formatting/type ratchets — closed:** both ratchet scripts were deleted once
  their underlying counts reached zero; direct checks now fail on any finding.

## Reproduction

```bash
make check
.venv/bin/python -m pip check
```
