# NIDAR RescueSwarm GCS backend
PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: help venv install dev-install migrate bootstrap run test test-unit test-integration lint fmt db db-test check

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

venv: ## Create the virtualenv
	python3.12 -m venv .venv

install: ## Install runtime dependencies
	$(PIP) install -r requirements.txt

dev-install: ## Install runtime + development dependencies
	$(PIP) install -r requirements-dev.txt

db: ## Start local PostgreSQL + PostGIS (development only)
	docker compose up -d postgis

db-test: ## Start the throwaway test database
	docker compose --profile test up -d postgis-test

migrate: ## Apply database migrations
	$(PY) -m alembic upgrade head

bootstrap: ## Create the first ADMIN account and sync the fleet registry
	$(PY) -m scripts.bootstrap --username admin

run: ## Run the backend (single worker; it holds live MAVLink links)
	$(PY) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

test-unit: ## Unit tests: no hardware, no database
	$(PY) -m pytest tests/unit -q

test-integration: ## Integration tests: needs TEST_DATABASE_URL
	$(PY) -m pytest tests/integration -q

test: test-unit ## Alias for the unit suite

lint: ## Lint
	$(PY) -m ruff check app tests scripts alembic

fmt: ## Auto-fix lint issues
	$(PY) -m ruff check --fix app tests scripts alembic

check: lint test-unit ## What CI runs
