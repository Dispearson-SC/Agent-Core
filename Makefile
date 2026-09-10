# Root-level task runner.
#
# Every command here is runnable from the repository root. That matters for more than
# convenience: tooling that inspects a workspace looks for a root-level declaration of how
# to test the project, and without one it concludes there is no test command at all — which
# is how this repo's fakes-first convention got reported as "no strict TDD" when the only
# real problem was a working directory.
#
# All development lives under Core/. These targets are thin wrappers so nobody has to know
# that to run the suite.

CORE := Core
PY   := python

.DEFAULT_GOAL := help
.PHONY: help install test test-unit test-integration lint typecheck check imports clean

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package plus dev dependencies
	cd $(CORE) && $(PY) -m pip install -e ".[dev]"

test:  ## Run the whole suite
	cd $(CORE) && $(PY) -m pytest

test-unit:  ## Unit tests only — no database, no network, no model
	cd $(CORE) && $(PY) -m pytest tests/unit

test-integration:  ## Integration tests — needs Postgres and DBOS
	cd $(CORE) && $(PY) -m pytest tests/integration

lint:  ## Ruff, including the banned-api layer rule
	cd $(CORE) && $(PY) -m ruff check src tests

typecheck:  ## mypy in strict mode
	cd $(CORE) && $(PY) -m mypy

imports:  ## Every module must import cleanly — runs without dev dependencies installed
	$(PY) $(CORE)/scripts/check_imports.py

check: imports lint typecheck test  ## Everything CI runs
	@echo "all checks passed"

clean:  ## Remove caches
	cd $(CORE) && rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
