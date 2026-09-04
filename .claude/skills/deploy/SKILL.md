---
name: deploy
description: The TailorCraft deploy runbook — build immutable images, push to GHCR, release to the single VDS behind the manual gate, plus rollback and the pre-deploy footgun checklist. Use when deploying, cutting a release, or debugging the CD pipeline.
---

# Deploying TailorCraft

Principle: **production never builds — it pulls immutable, SHA-tagged images** (ADR-0003, cicd.md).
Never `git pull` on the box.

## Normal release (via CI/CD)

1. Merge to `main` → CI green → the `build` job pushes `ghcr.io/<owner>/tailorcraft-api:<sha>` and
   `tailorcraft-web:<sha>` to GHCR.
2. The `deploy` job is bound to the GitHub **`production` Environment** with a required reviewer — it
   **waits for your approval**. Approve in the Actions UI to release.
3. On approval, over SSH to the VDS:

```bash
cd /home/tailorcraft-deploy/tailor-craft
# Persist the released tags so hand-run `docker compose` on the box targets the same images.
# ... TAILORCRAFT_API_IMAGE / TAILORCRAFT_WEB_IMAGE written into .env ...

docker compose pull api worker beat web        # every container that runs application code
docker compose up -d api web nginx
docker compose stop worker beat                # the migration window
docker compose exec -T api alembic upgrade head
docker compose up -d worker beat               # last, on the new image, against the migrated schema
```

4. **Verify the running images**, do not hope:

```bash
for svc in api worker beat; do
  running="$(docker compose ps -q "$svc" | xargs -r docker inspect -f '{{.Config.Image}}')"
  [ "$running" = "$TAILORCRAFT_API_IMAGE" ] || { echo "::error::$svc runs $running"; exit 1; }
done
```

5. **Smoke check:** `curl -fsS https://tailorcraft.app/health/ready` must return 200 — and it must
   report Postgres, Redis **and Celery**, or it is telling you less than you think.

## The two rules that step 3 and 4 exist for

**Every container that runs application code appears in the `pull` list and in the verification
loop.** In the previous project the background worker was in neither, so it ran a stale image for
four releases and the only symptom was behaviour not matching the source. Here that is `api`,
`worker`, and `beat`. A fourth daemon means edits in three places.

**The worker and beat stop across the migration**, not merely restart after it. They are the processes
that keep executing application code while the schema changes underneath them. Celery finishes the
task in flight on SIGTERM; queued tasks wait in Redis.

## Pre-deploy footgun checklist

- Traefik pinned to its network (`--providers.docker.network=traefik`).
- `docker-compose.yml` (prod) publishes **no** datastore ports; Redis has a password.
- Named volumes only (no stale anonymous Postgres volume).
- The **uploads volume** is mounted by both `api` and `worker`. Lose it on either and exports break in
  a way no health check sees.
- A fresh `make db.dump` exists before any migration that is not cleanly reversible.
- Migrations are additive (expand → migrate → contract) — the deploy runs two versions briefly.

## Rollback

Images are SHA-tagged in GHCR. Re-point the image tags in the box's `.env` to the previous SHA and
`docker compose up -d`. If a migration cannot be safely reversed, restore from the pre-deploy dump.

**The database dump is not a full backup of this product.** Restoring rows that point at uploaded
files you did not restore gives you a broken application with a green restore. Back up the uploads
volume alongside it.

## Manual first deploy / bootstrap

On a new box: create the external `traefik` network, create the root `.env` by hand (mode 600, with
the database and Redis passwords, `GEMINI_API_KEY`, and `JWT_SIGNING_KEY`), ensure the deploy user can
pull from GHCR, then run the steps above once by hand to verify. The production `.env` is never
shipped from CI.

## After a deploy that touched the purge job

The guest-retention job issues `DELETE` against rows **and** unlinks files (ADR-0006). If this release
changed it, rehearse rather than discover: `make purge.dry`, then `make purge limit=50`, then a full
run — see infrastructure.md's runbook.
