PYTHON ?= .venv/bin/python
LINT_IMPORTS ?= .venv/bin/lint-imports
QUALITY_PATHS := app tests scripts migrations
export PI_DATABASE_URL ?= sqlite+aiosqlite:////tmp/project-intelligence-backend-quality.db

.PHONY: check dependencies lint format format-check type-check imports test

check: lint format-check type-check dependencies imports test

lint:
	$(PYTHON) -m ruff check $(QUALITY_PATHS)

format:
	$(PYTHON) -m ruff format $(QUALITY_PATHS)

format-check:
	$(PYTHON) -m ruff format --check $(QUALITY_PATHS)

type-check:
	$(PYTHON) -m mypy app tests scripts migrations

dependencies:
	$(PYTHON) -m deptry . --exclude '.venv|\.git|\.local-sql' --optional-dependencies-dev-groups dev --known-first-party app,scripts --per-rule-ignores 'DEP002=aioodbc|aiosqlite|uvicorn|deptry|import-linter|mypy|pre-commit|pytest-cov|ruff,DEP004=pytest'

imports:
	$(LINT_IMPORTS) --no-cache

test:
	$(PYTHON) -m pytest --cov=app --cov-report=term-missing --cov-report=xml
