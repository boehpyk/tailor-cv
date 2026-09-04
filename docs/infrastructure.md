# TailorCraft Infrastructure Plan

One VDS, one Docker Compose stack, behind the Traefik instance already running on the box. Everything
here is chosen to be operable by one person at 11 p.m. without a runbook they have to invent first.

## Topology

```
                    internet
                       │  :443
                 ┌─────▼──────┐
                 │  Traefik   │  (shared, external `traefik` network, TLS via Let's Encrypt)
                 └─────┬──────┘
                       │
                 ┌─────▼──────┐
                 │   nginx    │  serves the built React bundle, proxies /api → api:8000
                 └──┬──────┬──┘
                    │      │
            ┌───────▼──┐  ┌▼──────────────┐
            │   api    │  │  static files │  (web build output, a named volume)
            │ FastAPI  │  └───────────────┘
            └──┬────┬──┘
               │    │
     ┌─────────▼┐  ┌▼──────────┐        ┌──────────┐      ┌──────────┐
     │ postgres │  │   redis   │◄───────┤  worker  │      │   beat   │
     └──────────┘  └───────────┘        │  Celery  │      │ Celery   │
                                        └────┬─────┘      │ schedule │
                                             │            └──────────┘
                                        ┌────▼─────┐
                                        │ uploads/ │  named volume, shared api ⇄ worker
                                        └──────────┘
```

**Five application containers, and each one matters at deploy time**: `nginx`, `api`, `worker`,
`beat`, plus the `web` build artifact. See [cicd.md](./cicd.md) — a container that runs application
code and is missing from the deploy's pull-and-verify list will silently run a stale image.

**The uploads volume is shared between `api` and `worker` on purpose.** The API writes an uploaded CV;
the worker reads it to render a PDF. This is the cheapest correct thing on one box, and it is also
the single assumption that must die first if this ever runs on two — which is exactly why every
access goes through `FileStorePort` (ADR-0006). The port is not ceremony; it is the seam where "one
box" stops being baked into the domain.

## Compose file strategy

Two files, and the split is a safety mechanism:

- `docker-compose.yml` — **production.** No datastore ports. No source bind mounts. Pulls immutable
  images. This is the file that ships to the box.
- `docker-compose.dev.yml` — **development.** Must be passed explicitly
  (`-f docker-compose.yml -f docker-compose.dev.yml`); Compose never auto-loads it under that name.
  It adds live source mounts, published ports bound to `127.0.0.1`, hot reload, and Mailpit later.

Dev-ness is a property of *loading the override file*, never of a value someone remembered to change
in a gitignored `.env`. `ENV` in the image and `.env.example` both default to the **safe**
(production) value; the override file is what makes a box a dev box.

## The footgun checklist (design against these — they have all bitten before)

1. **Traefik must be pinned to its network:** `--providers.docker.network=traefik`. Without it,
   Traefik may pick the wrong container IP and you get a silent 30-second 504 with nothing in any log.
2. **Docker bypasses UFW.** It writes iptables rules directly, so a `ports:` mapping is a hole in a
   firewall you believe is closed. Datastores publish **no** ports in production; the dev override
   binds to `127.0.0.1` only. The pre-commit hook enforces this — keep it working.
3. **Named volumes only.** An anonymous Postgres volume remembers a stale password and you will spend
   an hour proving the password in `.env` is correct while a volume from last week disagrees.
4. **Redis always has a password** and a stable network alias. It is the broker, the result backend,
   the cache and the rate-limiter store — four load-bearing jobs in one unauthenticated-by-default
   process.
5. **Health checks probe dependencies**, never `return "ok"`. See below.
6. **Bound the worker's life.** WeasyPrint and PDF rendering are the kind of workload that leaks; run
   the worker with `--max-tasks-per-child` and let `restart: unless-stopped` handle the recycle. A
   supervisor you do not have to install is a supervisor that cannot be misconfigured.
7. **The container writes into a live-mounted source tree as root** in dev, so files it creates become
   un-manageable by host git. Run the dev containers with a `user:` mapping to your UID, or expect to
   `chown 1000:1000` at annoying moments.

## Health checks

`/health/live` — the process is up. Nothing else. Used by Docker's healthcheck.

`/health/ready` — the process can do its job. It probes:

| Probe | Why | Failure |
|---|---|---|
| Postgres | `SELECT 1` | 503 |
| Redis | `PING` | 503 |
| Celery | at least one worker responds to `ping` **and** the queue is not stale | 503 |
| `jobs.guest_purge` | last-run heartbeat + current backlog count | 200 with `stale: true` |

The Celery probe is not optional garnish. **A stopped worker looks exactly like a healthy system** if
you only probe the datastores — the API answers, the database answers, and every export silently
queues forever. The previous project learned this the expensive way, with a crash-looping worker and
a green dashboard. Probe the thing that can be down.

## Guest data retention (runbook)

FR-6 and Constitution §7 both say the same thing: **guest data lives at most 24 hours.** A Celery Beat
job sweeps it.

```bash
# Report only. Deletes nothing. Read the counts before you trust the job.
make purge.dry

# A small, explicit bite. Confirm the counts move by exactly what you asked for.
make purge limit=50

# A full run.
make purge

# The signal to trust — the backlog, not the log line.
curl -s localhost:8080/health/ready | jq .jobs.guest_purge
```

### First run — rehearse it, do not discover it

This job issues `DELETE` against rows **and** unlinks files. Both are irreversible and the file half
is not covered by the database backup you take before migrations. Run the three steps above in order,
on real data, by hand, before the Beat schedule is switched on (see the roadmap's deferred list).

### What to check when the backlog grows

| Symptom | Means |
|---|---|
| `overdue` climbing | Nothing is sweeping. Check that `beat` is running *and* on the current image. |
| `stale: true` | No heartbeat for > 3 h. The schedule fires hourly; three misses is a real fault. |
| `last_run: null` | The job has never run, or Redis was flushed. Both are worth knowing. |
| Rows gone, files remaining | The delete order is wrong. Delete the file **after** the row commits, and make the file sweep able to find orphans on its own — a file with no row is invisible to a row-driven sweep. |

**A job whose only failure symptom is silence needs a signal that cannot be faked by a job that runs
and does nothing.** That is why the backlog count exists, and why the job logs one line on every run
*including* the runs that delete zero rows. "No log output" must never be ambiguous between "healthy
and idle" and "not running".

## Secrets & configuration

| Variable | Where it lives |
|---|---|
| `DB_*`, `REDIS_PASSWORD` | the box's root `.env`, mode 600, created by hand once |
| `GEMINI_API_KEY` | same. **Never in an image layer, never in CI.** |
| `JWT_SIGNING_KEY` | same. Rotating it logs everyone out — that is the intended behaviour. |
| `SSH_*`, `GHCR_TOKEN` | GitHub Actions secrets only |

Read the environment in **one** place (a settings object). Nothing else calls `os.environ`. A config
value that can be read from anywhere will eventually be read from the domain layer.

## Backups & recovery

- `make db.dump` before every migration that is not trivially reversible. The deploy runbook makes
  this a step, not a suggestion.
- Nightly `pg_dump` **plus the uploads directory**, off the box. The database alone is not a backup of
  this product: a restored row pointing at a file that no longer exists is a broken application with
  a green restore.

## Monitoring & observability

- **Sentry from Phase 0** (roadmap). This product has silent failure paths from its first slice.
- Structured JSON logs to stdout, collected by Docker. One request id threaded from the HTTP boundary
  through to the Celery task, or a failed export is untraceable back to the user who asked for it.
- **Never log CV content or LLM prompt bodies.** Log the ids, the sizes, the durations, the token
  counts. Constitution §8 — a debug log is the easiest way to leak a stranger's address and phone
  number into a file nobody thinks of as a database.

## Environments

| Environment | How it runs |
|---|---|
| dev | `make up.dev` — both compose files, live mounts, ports on `127.0.0.1`, hot reload |
| test | `pytest` against `tailorcraft_test`, a dedicated database, never the dev one |
| production | `docker-compose.yml` only, immutable GHCR images, behind Traefik |
