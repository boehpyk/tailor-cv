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

deps: ## Sync dependencies from the lockfiles — in EVERY container running application code
	# api, worker AND beat. All three mount the same source and run the same package, so a sync
	# that reaches only `api` leaves the other two on a venv from whenever they were last built.
	# That is not hypothetical: slice 1.3 found `worker` and `beat` still holding a venv from
	# slice 1.2, which went unnoticed only because the posting fetch happens to run in the API.
	# The Gemini call does not — it runs in the worker. Same failure shape as the previous
	# project's worker running a stale image for four releases: no error, just behaviour that
	# does not match the source.
	for c in api worker beat; do $(DC_DEV) exec -T $$c uv sync --frozen; done
	# `web` too: its node_modules is an anonymous volume, so a dependency added to package.json on the
	# host is invisible to the container until something installs it there (slice 1.4, F1).
	$(DC_DEV) exec -T web npm ci

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
	@# `createdb` rather than a SELECT-then-CREATE dance: three levels of quoting (make -> sh ->
	@# psql) is how you end up asking for a database called 'tailorcraft'_test. Re-running is a
	@# no-op, and if creation genuinely fails, pytest's next connection error says so loudly.
	@$(DC_DEV) exec -T postgres sh -c 'createdb -U "$$POSTGRES_USER" "$${POSTGRES_DB}_test" 2>/dev/null || true'
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
	# `-b` is load-bearing: web/tsconfig.json is solution-style (`files: []` + references), and a bare
	# `tsc --noEmit` on it type-checks ZERO files. It passed a deliberate `const x: number = "nope"`
	# for four slices (found at 1.4's F8). A gate that checks nothing also supplies confidence.
	$(WEB) npx tsc -b --noEmit

web.lint: ## ESLint
	$(WEB) npm run lint

web.format: ## Format the frontend (prettier)
	$(WEB) npm run format

web.format.check: ## Check frontend formatting without changing files
	$(WEB) npm run format:check

web.test: ## Vitest
	$(WEB) npx vitest run

web.build: ## Production build (a build failure is a deploy failure)
	@# NODE_ENV is forced here. The dev container sets NODE_ENV=development (it runs the Vite dev
	@# server), and Vite honours it during `build` too — so without this line the local gate quietly
	@# checked a React DEVELOPMENT bundle, 442 kB where production ships 236 kB. It passed, and it
	@# was not checking the artifact CI and the box actually build. Local and CI stay in lockstep
	@# (docs/cicd.md); a gate that checks a different thing is worse than no gate.
	$(DC_DEV) exec -T -e NODE_ENV=production web npm run build

web.check: web.types web.lint web.format.check web.test web.build ## All frontend gates

#-----------------------------------------------------------
# The Definition-of-Done chain. Must stay in lockstep with .github/workflows/ci.yml.
#-----------------------------------------------------------
check: lint types imports test web.check ## Run all quality gates — run before every commit

# check.static exists for exactly one job: the RED commit of a tiered-TDD cycle (docs/sdlc.md §2),
# where a new test is *supposed* to be failing. Every other gate still applies — a red test is still
# Ruff-clean, mypy-clean and does not break the frontend build. Do NOT reach for this to get a commit
# past a test you have not fixed; that is exactly what it looks like from the outside, which is why
# the pre-commit hook refuses TDD_RED=1 unless a test file is actually staged.
check.static: lint types imports web.types web.lint web.format.check web.build ## All gates except pytest/vitest — RED commits only
	@echo "check.static: OK (pytest and vitest deliberately NOT run)"

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
