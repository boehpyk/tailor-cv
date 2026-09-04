# TailorCraft CI/CD

Trunk-leaning GitHub Flow, CI on every PR, and a **manual gate** in front of production. The whole
pipeline is built around one rule: **production never builds — it pulls an immutable, SHA-tagged
image** (ADR-0003). Never `git pull` on the box.

## Branching model — GitHub Flow

```
main ──●────────────●───────────────●──────►  always releasable, protected
        \          /                 \
         ● feature/intake-base-cv ───●  squash-merge via PR (CI green)
```

- `main` is protected: no direct pushes, PR required, CI must pass.
- Branch names: `feature/<slice-name>` or `fix/<what>`, matching the spec folder in `docs/specs/`.
- Squash-merge. One slice, one commit on `main`, one line in the history that means something.
- Even solo, **open the PR.** It is where CI runs and where you read your own diff as a stranger
  would. The 90 seconds it costs is the cheapest review you will ever get.

## Pipeline

### CI — on every PR and push to `main` (`.github/workflows/ci.yml`)

Two jobs, run in parallel, because a Python failure and a TypeScript failure are independent news:

| Job | Steps |
|---|---|
| `api` | `ruff format --check` → `ruff check` → `mypy --strict` → `lint-imports` → `alembic upgrade head` against a service Postgres → `pytest` |
| `web` | `tsc --noEmit` → `eslint` → `vitest run` → `vite build` (a build that fails is a deploy that fails) |

The job list must equal `make check`. **That equality is a drift seam** and it is the one CI mistake
that is invisible: a gate flag added to the Makefile and not to CI (or the reverse) produces a green
pipeline that is not checking what you think it checks. When you change a gate, change both, in the
same commit.

`pytest` never calls the real Gemini API. CI has no `GEMINI_API_KEY`, deliberately — a suite that
would silently start spending money if a key appeared is a suite that is one secret away from a
surprise bill. The fake `LlmPort` is the only implementation tests see.

### Phase-gate jobs (added as phases land)

- **Phase 1:** a parsing-robustness job over the committed corpus of sample CV layouts (PRD §10
  validation 1) — it asserts extraction does not crash and yields non-empty text, not that the text
  is *good*.
- **Phase 1:** an export smoke job that renders a fixture document to PDF and DOCX and asserts the
  files are non-trivial and open (the > 98 % success metric needs a floor under it).

### CD — build on merge, deploy behind a manual gate (`.github/workflows/deploy.yml`)

1. **`build`** (automatic on merge to `main`): builds `docker/api/Dockerfile` and
   `docker/web/Dockerfile`, pushes `ghcr.io/<owner>/tailorcraft-api:<sha>` and
   `…-web:<sha>` (plus `:latest`) to GHCR.
2. **`deploy`** (bound to the GitHub `production` Environment with a **required reviewer** — that
   approval *is* the gate): syncs the compose file and nginx conf, then over SSH:
   - write the released image tags into the box's `.env`,
   - `docker compose pull api worker beat web`,
   - `docker compose up -d api web nginx`,
   - **stop the worker and beat** for the migration window,
   - `alembic upgrade head`,
   - bring the worker and beat back up on the new image,
   - **assert every container is running the image just released**, and fail loudly if not.

That last step exists because of a specific, expensive bug in the previous project: the deploy
updated the web container and not the background worker, so the worker ran a stale image *for four
releases* and the only symptom was behaviour that did not match the source. **Every long-running
container that runs application code must appear in both the `pull` list and the verification loop.**
Here that means `api`, `worker`, and `beat`. Adding a fourth daemon means adding it in three places.

The worker is **stopped across the migration**, not merely restarted after it, because it is the one
process that keeps executing application code while the schema changes underneath it. Celery finishes
the task in flight on SIGTERM; queued tasks wait in Redis.

3. **Smoke check:** `curl -fsS https://tailorcraft.app/health/ready` must return 200.

Migrations are additive and backward-compatible (expand → migrate → contract), so the brief overlap
of old and new containers never breaks.

## Rollback

Images are SHA-tagged in GHCR. Re-point the image tags in the box's `.env` to the previous SHA and
`docker compose up -d`. If a migration cannot be safely reversed, restore from the pre-deploy dump —
which is why `make db.dump` is a step in the deploy runbook, not a suggestion.

## Secrets

Stored as GitHub Actions secrets, never in the repo:

| Secret | Used by |
|---|---|
| `SSH_HOST`, `SSH_USER`, `SSH_DEPLOY_KEY` | the deploy job |
| `GHCR_TOKEN` | `docker login` on the box |

The production `.env` (database password, Redis password, `GEMINI_API_KEY`, JWT signing key) is
created **on the box, by hand, once**, and is never shipped from CI. CI has no production database
credentials and no Gemini key.

## Notifications

Until Sentry is wired (Phase 0), a failed deploy is only visible in the Actions tab. That is the
weakest link in this pipeline and it is why Sentry is not deferred here — see
[roadmap.md](./roadmap.md).
