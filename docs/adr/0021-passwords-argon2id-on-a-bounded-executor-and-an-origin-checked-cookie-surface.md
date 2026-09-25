# ADR-0021: Passwords — argon2id with fixed parameters on a bounded executor, decoy verification, and an `Origin`-checked cookie surface

- **Status:** Accepted
- **Date:** 2026-09-23
- **Relates to:** ADR-0008 (argon2id was decided there; this ADR decides how it runs, and its
  amendments (a), (b) and (e)), ADR-0009 (CPU-bound work off the loop), ADR-0012 (no off switch; a
  testing seam is a constructor argument with a strict default), ADR-0010 §3 (entropy decides the
  hash). Supersedes nothing.

## Context

Slice 2.1 is the first time this product holds a password, and the first time it runs a CPU-bound
call whose whole purpose is to be slow. Four forces:

1. **A synchronous KDF inside an async route stalls every concurrent user** for ~100 ms per login
   and logs nothing. This codebase has already met that shape three times (DOCX sniffing,
   WeasyPrint, the orphan scanner).
2. **argon2's cost is memory as well as time.** At 64 MiB per in-flight hash, an unbounded pool is a
   memory bomb that a login burst can set off on a small VDS.
3. **An unknown email must cost the same as a wrong password**, or response time answers the
   question the response body refuses to (ADR-0008 amendment (b)).
4. **The refresh cookie is the one credential the browser sends by itself**, and `cv.samolit.com`
   shares its registrable domain with sibling apps. `SameSite` cannot see a same-site sibling.

## Decision

**1. argon2id through `argon2-cffi`, with parameters that are module constants:** `memory_cost=65536`
KiB, `time_cost=3`, `parallelism=4`, `hash_len=32`, `salt_len=16` (RFC 9106's low-memory profile,
the library's default, passed **explicitly** so a library upgrade cannot move them). **No setting
exists.** Tests use cheap parameters through a constructor argument whose default is the strict set;
no environment variable can weaken a control. `argon2` is imported by exactly one module and sits on
both import-linter forbidden lists.

**2. Hashing and verification run on a dedicated `ThreadPoolExecutor(max_workers=2)`**, created in the
app's lifespan and shut down with it. Not `asyncio.to_thread`: that shares the loop's default
executor with CV extraction and the posting parser, so a login burst would starve an upload, and it
is unbounded against memory. **The executor's size is the memory cap** — 2 per process × 2 uvicorn
processes = 256 MiB worst case. argon2-cffi releases the GIL, so two hashes genuinely run in
parallel. A burst waits in the executor's queue; the rate limiters bound the queue.

**3. Decoy verification.** The adapter computes one decoy hash at construction, with the live
parameters, and `verify(password, None)` — "no such account" — verifies against it and returns
`MISMATCH`. The port's docstring says `None` is not an optimisation opportunity.

**4. Passwords are NFKC-normalised, never stripped, 12–128 code points at registration, and capped at
1024 characters everywhere** (the hashing-DoS bound, which also applies to login). No composition
rules: they push users toward predictable patterns and NIST SP 800-63B rev. 4 forbids them. A
password equal to the email or its local part is refused.

**5. Login and registration are rate-limited before any hash is computed, and the limiters fail
closed** (ADR-0008 amendment (e)): login 20/h per IP and 10/h per email, registration 5/h per IP.
The per-email key is `HMAC-SHA256(k_rl, normalized email)` with `k_rl` derived from
`JWT_SIGNING_KEY` under a fixed label — a derived subkey, so the signing key is never used directly
for a second purpose and the email never appears in a Redis key.

**6. The four cookie-touching endpoints (`register`, `login`, `refresh`, `logout`) require a trusted
`Origin`** — `PUBLIC_BASE_URL` or a member of `CORS_ORIGINS`, exact match — checked **before** the
limiter, the database and the hasher. Missing or foreign is 403 `origin_not_allowed`. The layers, each
named for the attack it stops: `SameSite=Strict` stops cross-*site* requests; host-only keeps a
sibling from *receiving* the cookie; `Path=/api/auth` keeps it off every other request; the `Origin`
check stops a same-site sibling *sending* one, and stops login CSRF; JSON-only bodies stop a
cross-origin `<form>`. `/me` is exempt — it is authorised by a bearer header a cross-site request
cannot attach.

## Alternatives

- **bcrypt.** Rejected: it silently truncates at 72 bytes, and a 1024-character cap means a user can
  type a password bcrypt would not fully read.
- **scrypt.** Sound, but argon2id is the current recommendation (RFC 9106, OWASP) and the ADR had
  already named it.
- **Parameters as settings.** Rejected: a setting that weakens a control is an off switch, and this
  codebase does not ship one (ADR-0012).
- **`asyncio.to_thread`.** Rejected in (2): shared with unrelated CPU work, unbounded against memory.
- **A Celery task.** Rejected: ADR-0005's rule is the cost of the work, and ~100 ms that must finish
  before the response is not queue-shaped — a login that queued and polled would be a worse login.
- **A synchroniser (double-submit) CSRF token.** Rejected: it needs a second cookie readable by
  JavaScript — a readable credential on the page the whole design keeps credentials off. The `Origin`
  check buys the same guarantee with nothing readable.
- **Account lockout after N failures.** Rejected: lockout is a denial-of-service against the victim,
  performed by anyone who knows their email. Rate limiting bounds the guesser without handing them
  that.

## Consequences

- **Redis down stops new logins and registrations** (503 `rate_limit_unavailable`); it never logs
  anyone out, because refresh, logout and `/me` do not touch Redis.
- **A client with no `Origin` header cannot log in.** Every modern browser sends one on a `POST`; a
  non-browser client (and every test) must set it. A developer hitting Vite directly on `:5173` gets
  403 — use `:8080`, as every other slice does.
- **Timing equality is measured, not assumed**: at `/verify`, on the production image with production
  parameters, unknown-email and wrong-password logins must have medians within 10 %. CI cannot measure
  it honestly with cheap test parameters; it proves the structural half (exactly one verify call).
- **If the VDS's memory is tight, the executor drops to 1 before the parameters drop.** A constant
  change, recorded when it happens.
- **A `wait_for` around the executor future would cancel the waiter, never the running thread.** The
  bound on a burst is the rate limit, not a timeout.
