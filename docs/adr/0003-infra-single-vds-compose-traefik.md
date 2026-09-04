# ADR-0003: One VDS, Docker Compose, behind the shared Traefik

- **Status:** Accepted
- **Date:** 2026-09-04

## Context

The product's launch audience is the founder and a friend group (PRD §8). Monetization is a *later*
validation step, not a launch requirement. Meanwhile there is already a VDS running other projects
behind a shared Traefik instance, and one person to operate everything.

The temptation in an AI-flavoured product is to reach for managed everything — a serverless API, a
hosted queue, an object store — and arrive at a monthly bill and four consoles before the first user.

## Decision

**One VDS. One Docker Compose stack. Behind the Traefik instance already on the box.**

- Traefik terminates TLS and routes by host to this stack's `nginx`, over a shared **external**
  `traefik` network.
- `nginx` serves the built React bundle and proxies `/api` to the `api` container.
- Postgres and Redis run as containers with **named** volumes and **no published ports** in
  production.
- Uploaded and generated files live on a **named volume shared between `api` and `worker`**
  (ADR-0006), not in object storage.
- Production **pulls immutable SHA-tagged images from GHCR** and never builds. Deploy is a GitHub
  Actions job behind a required-reviewer gate (see cicd.md).

## Alternatives

- **Managed Postgres + Redis + S3 + a PaaS.** Less to operate, more to pay, and — for a learning
  project — most of the interesting failure modes hidden behind someone else's dashboard. Rejected
  for now; the `FileStorePort` is the seam that makes S3 a later adapter rather than a migration.
- **Kubernetes.** Rejected outright (Constitution §5). One box, one Compose file.
- **A second VDS for the worker.** Rejected: the shared uploads volume is the cheap correct answer at
  this scale, and splitting hosts would force the file-storage decision before there is a reason.
- **Building on the production box.** Rejected. It couples deploy to a working toolchain on the
  server, makes rollback a rebuild, and means the artifact you tested is not the artifact you ran.

## Consequences

- **Docker bypasses UFW.** Every `ports:` line is a firewall decision. Production publishes none for
  datastores; dev binds to `127.0.0.1`. A pre-commit hook enforces this because the failure is silent
  and remote.
- Traefik **must** be pinned with `--providers.docker.network=traefik`, or routing intermittently
  targets the wrong container IP and produces a 30-second 504 with no log line anywhere.
- Vertical scaling only. The known ceiling is concurrent PDF rendering (PRD §10 validation 3); when it
  is reached the answer is worker concurrency and then a bigger box, in that order.
- Rollback is re-pointing an image tag and `up -d`. This is the main payoff of never building on prod
  and is worth rehearsing once before it is needed.
