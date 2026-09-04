.DEFAULT_GOAL := help

DC      := docker compose
DC_DEV  := docker compose -f docker-compose.yml -f docker-compose.dev.yml
API     := $(DC_DEV) exec -T api
WEB     := $(DC_DEV) exec -T web
DUMP_DIR := backups

#-----------------------------------------------------------
# Stack
#-----------------------------------------------------------
up.dev: ## Start the full stack (dev: live mounts, hot reload, 127.0.0.1 ports)
	$(DC_DEV) up -d --build

down.dev: ## Stop the dev stack
	$(DC_DEV) down

up.prod: ## Start in production mode (no override file — never auto-loaded)
	$(DC) up -d

down.prod: ## Stop the prod stack
	$(DC) down

logs: ## Tail logs (usage: make logs [c=api])
	$(DC_DEV) logs --tail=200 -f $(c)

shell: ## Shell into the api container
	$(DC_DEV) exec api bash

#-----------------------------------------------------------
# Application (Python / Alembic / Celery)
#-----------------------------------------------------------
migrate: ## Apply Alembic migrations
	$(API) alembic upgrade head

migration.make: ## Autogenerate a migration (usage: make migration.make name="add base cv") — THEN READ IT
	$(API) alembic revision --autogenerate -m "$(name)"
	@echo ""
	@echo "  Autogenerate is a DRAFT (ADR-0007). Read every line before committing it."

migration.down: ## Roll back one migration
	$(API) alembic downgrade -1

deps: ## Sync Python dependencies from uv.lock
	$(API) uv sync --frozen

db.dump: ## Dump the database (custom format) to backups/
	@mkdir -p $(DUMP_DIR)
	@out="$(if $(file),$(file),$(DUMP_DIR)/tailorcraft_$$(date +%Y%m%d_%H%M%S).dump)"; \
	echo "Dumping to $$out ..."; \
	if $(DC) exec -T postgres sh -c 'pg_dump -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -Fc --no-owner --no-privileges' > "$$out.part"; then \
		mv "$$out.part" "$$out"; echo "Done: $$out"; \
		echo "NOTE: this is the database only. The uploads volume is NOT in here (ADR-0006)."; \
	else rm -f "$$out.part"; echo "pg_dump failed." >&2; exit 1; fi

#-----------------------------------------------------------
# Guest retention (see docs/infrastructure.md — rehearse, do not discover)
#-----------------------------------------------------------
purge.dry: ## Report what the guest purge WOULD delete. Deletes nothing.
	$(API) python -m tailorcraft.cli purge-guests --dry-run

purge: ## Run the guest purge (opts: limit=N for a small, explicit bite)
	$(API) python -m tailorcraft.cli purge-guests $(if $(limit),--limit=$(limit))

#-----------------------------------------------------------
# Quality gates — backend
#-----------------------------------------------------------
fmt: ## Format Python (ruff format)
	$(API) ruff format .

lint: ## Lint + format check (ruff)
	$(API) ruff format --check .
	$(API) ruff check .

types: ## Static analysis (mypy --strict)
	$(API) mypy .

imports: ## Enforce hexagonal layer boundaries (import-linter)
	$(API) lint-imports

test.db: ## Create the dedicated test database if it does not exist
	@$(DC_DEV) exec -T postgres sh -c 'psql -U "$$POSTGRES_USER" -d postgres -tc \
		"SELECT 1 FROM pg_database WHERE datname = '"'"'$$POSTGRES_DB'"'"'_test" \
		| grep -q 1 || psql -U "$$POSTGRES_USER" -d postgres -c \
		"CREATE DATABASE $$POSTGRES_DB""_test"'
	@echo "test database ready"

test: test.db ## Run the backend suite (opts: k=<expr>, file=<path>) — provisions the test DB first
	$(DC_DEV) exec -T -e APP_ENV=test api pytest $(if $(k),-k "$(k)") $(file)

test.twice: ## Run the suite twice. A second run that fails means state leaked (usually Redis).
	$(MAKE) test
	$(MAKE) test

#-----------------------------------------------------------
# Quality gates — frontend
#-----------------------------------------------------------
web.types: ## TypeScript check
	$(WEB) npx tsc --noEmit

web.lint: ## ESLint
	$(WEB) npm run lint

web.format: ## Format the frontend (prettier)
	$(WEB) npm run format

web.format.check: ## Check frontend formatting without changing files
	$(WEB) npm run format:check

web.test: ## Vitest
	$(WEB) npx vitest run

web.build: ## Production build (a build failure is a deploy failure)
	$(WEB) npm run build

web.check: web.types web.lint web.format.check web.test web.build ## All frontend gates

#-----------------------------------------------------------
# The Definition-of-Done chain. Must stay in lockstep with .github/workflows/ci.yml.
#-----------------------------------------------------------
check: lint types imports test web.check ## Run all quality gates — run before every commit

#-----------------------------------------------------------
# LLM evaluation (NOT a test — it calls the real API and costs money)
#-----------------------------------------------------------
eval: ## Run the prompt eval set against the real Gemini API, by hand (ADR-0004)
	@echo "This calls the real API and costs money. It is not part of 'make check'."
	$(API) python -m tailorcraft.cli eval-prompts

#-----------------------------------------------------------
# Git hooks
#-----------------------------------------------------------
hooks.install: ## Point git at the tracked hooks in scripts/git-hooks/
	git config core.hooksPath scripts/git-hooks
	chmod +x scripts/git-hooks/*
	@echo "core.hooksPath -> $$(git config core.hooksPath)"

#-----------------------------------------------------------
# Help
#-----------------------------------------------------------
help: ## Show this help
	@grep -E '^[a-zA-Z_.-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: up.dev down.dev up.prod down.prod logs shell migrate migration.make migration.down deps \
        db.dump purge.dry purge fmt lint types imports test.db test test.twice web.types web.lint \
        web.format web.format.check web.test web.build web.check check eval hooks.install help
