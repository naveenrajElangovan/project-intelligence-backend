PYTHON ?= .venv/bin/python
LINT_IMPORTS ?= .venv/bin/lint-imports
QUALITY_PATHS := app tests scripts migrations
STRICT_MYPY_MODULES := app/application/chat_policy.py app/auth/models.py app/authorization/models.py app/telemetry.py
export PI_DATABASE_URL ?= sqlite+aiosqlite:////tmp/project-intelligence-backend-quality.db

.PHONY: check lint format-check type-check imports test

check: lint format-check type-check imports test

lint:
	$(PYTHON) -m ruff check $(QUALITY_PATHS)
	$(PYTHON) -m ruff check --select I scripts/check_mypy_ratchet.py scripts/check_ruff_format_ratchet.py

format-check:
	$(PYTHON) scripts/check_ruff_format_ratchet.py

type-check:
	$(PYTHON) -m mypy --strict $(STRICT_MYPY_MODULES)
	$(PYTHON) scripts/check_mypy_ratchet.py

imports:
	$(LINT_IMPORTS) --no-cache

test:
	$(PYTHON) -m pytest --cov=app --cov-report=term-missing --cov-report=xml
