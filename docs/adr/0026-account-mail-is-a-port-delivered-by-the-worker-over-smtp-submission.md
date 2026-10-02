# ADR-0026: Account mail is a port, delivered by the worker over SMTP submission

- **Status:** Accepted
- **Date:** 2026-10-02
- **Relates to:** ADR-0004 (everything external crosses a port), ADR-0005 (the queue topology —
  **amended** for a fourth queue, `mail`), ADR-0008 (b) (*"until an email channel exists"* — this ADR
  is that channel), ADR-0010 §3 and ADR-0020 §7 (a 256-bit token is stored as SHA-256), ADR-0012 (the
  guarded egress — and why this is not one), ADR-0014 §5, §6 and §8 (commit then enqueue; one retry
  mechanism, in the adapter; a context-specific queue port), ADR-0027 and ADR-0028 (the two flows
  that send mail). Constitution §3 and §8 are amended in the same commit. Supersedes nothing.

## Context

Until slice 2.5 the email address an account holds never left the box. Registration enumerated
(ADR-0008 (b)) because the only design that does not — *always 202, and tell the address's owner by
mail* — needed a channel that did not exist. Slice 2.5 builds it, and with it a password reset, which
has no other honest proof than reading mail at the address.

Six forces meet:

1. **A new third party sees personal data.** The mail provider receives the recipient address and a
   message carrying a credential (a one-time link). The LLM precedent (Constitution §8) is that a third
   party which sees PII is *stated to the user*, not buried.
2. **A mail credential is a bearer secret.** Whoever reads the link can create the account or set
   its password. The plaintext must exist in as few places as possible: not in a broker message, not
   in a log, not in Sentry, not in an access log.
3. **Mail is slow and fails in its own ways** — a provider that is down, one that throttles, one that
   refuses our credentials, a recipient that does not exist. None of that may turn a registration
   request into a 500, and none of it may stall the event loop (`smtplib` is synchronous).
4. **A person is waiting with nothing to watch.** Unlike a tailoring run there is no spinner; the
   user is looking at an inbox.
5. **Dev and CI must not be able to mail a stranger**, and a dev box with production-shaped
   credentials in its `.env` is the Gemini footgun in a new shape (a click at localhost acting on the
   world).
6. **The vendor is the owner's choice and has not been made yet** (OQ-5). The design must not depend
   on it.

## Decision

**1. `AccountMailPort` speaks identity's language, not SMTP's.** One method, `send(mail: AccountMail)
-> None`, raising `MailNotDelivered(reason, smtp_code)`. `AccountMail` is a union of three frozen
values — `ConfirmYourEmail(to, token, expires_in)`, `AccountAlreadyExists(to)`,
`ResetYourPassword(to, token, expires_in)` — in `domain/identity/account_mail.py`. The domain says
*which* message and *what it carries*; subject lines and wording are presentation and live in the
adapter. The four failure reasons are a domain enum: `recipient_rejected`, `unavailable`,
`throttled`, `provider_refused`. No MIME, no host, no port, no retry count crosses the port. A fourth
message is a new union member, a template and a use case.

**2. The request records and enqueues; the worker delivers.** A registration or reset request commits
its row through a committing adapter, then publishes **one Celery task whose only argument is the
row's id** through `AccountMailQueuePort` (ADR-0014 §5's order and §8's shape: a context-specific
port, not the Constitution's generic `TaskQueuePort`). The task runs on its own queue, **`mail`**
(ADR-0005's amendment): mail is the one workload a person waits on with nothing to watch, and a
separate queue makes a dedicated mail worker a `-Q` change rather than a redesign. The task is a thin
entry point (ADR-0005): resolve, call the delivery use case, log one line with the outcome.

**3. The token is minted in the worker, committed, then sent — in that order.** If the route minted
the token, its plaintext would ride in a Redis message beside an id until a worker took it. Instead
the worker calls `OneTimeTokenPort.mint()`, stores the **hash** on the row, **commits**, and only then
hands the plaintext to `AccountMailPort.send`. The plaintext exists in one process's memory and in one
mail. This is the one place a plaintext credential crosses `application/`: it is typed
`OneTimeToken`, masked in `repr`/`str`/`format`, and readable only through `.reveal()`, which the mail
adapter alone calls.

| Order | A crash between the two | Consequence |
|---|---|---|
| send, then commit | mail delivered, hash not stored | a **dead link** in the inbox, and a redelivery mails a second one |
| **commit, then send** (chosen) | hash stored, mail not sent | **no mail**; a redelivery finds the row issued and skips; the user's *Send it again* recovers |

Every link that is mailed works. A row is **issued at most once per request** (the aggregate refuses
a second issue, and the repository's `UPDATE … WHERE token_hash IS NULL` refuses it in the database),
so a redelivery after a successful send is a no-op: at most one mail per request, never a duplicate.

**4. SMTP submission, on the standard library.** Port 587 with STARTTLS **required** (or implicit TLS
on 465), TLS verification on through `ssl.create_default_context()`, `smtplib` and `email.message`
from the standard library — **no new dependency**. SMTP because every transactional provider speaks
it, so the vendor is a setting rather than an adapter; because dev and CI then exercise the
**production adapter** against a real SMTP server instead of a fake standing in for a vendor's HTTP
client; and because its failure taxonomy is the protocol's own (4xx transient, 5xx permanent, 535
authentication) rather than one vendor's JSON. A server that does not offer STARTTLS is
`unavailable`, never a plaintext fallback; `MAIL_SMTP_SECURITY=none` exists for Mailpit and is refused
in production.

**5. It is not a guarded egress, and the obligations that still apply are applied.** ADR-0012's ten
obligations exist because the **caller chooses the host**. Here the host is a setting read once at
startup; no request can name it, and SMTP has no redirect to follow. What remains true of any
outbound call is kept: a per-attempt socket timeout (`MAIL_SEND_TIMEOUT_SECONDS=10`), a total deadline
(`MAIL_TOTAL_DEADLINE_SECONDS=30`, refused at or above Celery's soft limit), TLS verification, a
testing seam that is a constructor argument with a strict default (`connect=`, ADR-0012's rule), and
an `except Exception` floor that makes the port's promise true by construction. The adapter logs the
exception **type** and the SMTP **reply code** only — never the reply text, which servers use to echo
the recipient — and re-raises `from None`. `smtplib`'s `debuglevel` is pinned to 0.

**6. Each attempt runs in a thread, and the timeout is the socket's.** `smtplib` blocks, so every
attempt runs under `asyncio.to_thread`. The worker's loop serves one task at a time, so this is the
house rule rather than a measured stall. Because `wait_for` cannot cancel a thread (slice 1.5's
lesson), the bound that actually holds is the socket timeout per attempt, the bounded number of
attempts, and Celery's hard limit outside both.

**7. One retry mechanism, in the adapter** (ADR-0014 §6). At most `MAIL_MAX_ATTEMPTS=2` connections
per send, a short backoff, inside the total deadline, and only for `unavailable` and `throttled`.
The task declares no `autoretry_for` and **no `countdown`**: on Redis a delayed message is restored
every `visibility_timeout` until it runs. When the adapter gives up the delivery is a recorded
outcome (`FAILED` with its reason), logged, and the user's recovery is *Send it again*. A
`provider_refused` (our credentials or our sender refused) is logged at **error** level, because it
fails every mail until an operator acts. A `recipient_rejected` deletes the row: it has no future.

**8. Mailpit in dev and CI, a recording adapter in tests, and no adapter switch in production.**
`docker-compose.dev.yml` pins `MAIL_SMTP_HOST=mailpit`, port 1025 and `MAIL_SMTP_SECURITY=none` in
`environment:`, which outranks `env_file:` — so a root `.env` holding production `MAIL_*` values still
delivers to Mailpit in dev. Mailpit runs in dev and CI only, publishes its UI on the loopback and its
SMTP port not at all. Tests replace the adapter on the worker's composition-root path with a
recording double that lives under `tests/`, exactly as the LLM fake is injected. There is no setting
that selects a fake in production, because a setting that can turn a control off is an off switch.

**9. Plain text, transactional only, tracking off, and nothing else is ever mailed.** The body of a
confirmation or reset mail is a one-time link and expiry wording; the account-exists notice carries
two plain links and no token. No name, no CV content, no account id. The link carries the token in
the URL **fragment** (ADR-0027), never a query string. Open and click tracking must be **off** at the
provider: click tracking would route the token through the provider's redirector and log it there.
No HTML part, no marketing stream, no notifications.

**10. The provider, its payload and its retention.** The protocol is decided here; **the vendor is not
yet named.** It is chosen by the owner before the first production send (task T44 of slice 2.5) and
recorded as an amendment to this ADR, with the content-retention setting it was given and the
tracking setting verified off. The criteria it is chosen against: a transactional-only stream; SMTP
submission with STARTTLS; DKIM signing and a return path on our own domain; tracking that can be
switched off; the shortest content retention available; a data-processing agreement; EU data
residency where offered (the payload's only personal data is the address, and residency costs
nothing extra). The payload is what decision 9 says. The sender is an address on the application's
own subdomain, so its reputation is kept apart from the parent domain's; the owner controls that DNS
and publishes SPF, DKIM and a DMARC policy that starts at `p=none` with reports. The user is told,
where they type their address, that the provider sees it (Constitution §8).

**11. Five more startup refusals in production**, run by `check-settings` before uvicorn starts
(CLAUDE.md's pre-flight): `MAIL_SMTP_HOST` empty; `MAIL_SMTP_SECURITY=none`; `MAIL_SMTP_USERNAME` or
`MAIL_SMTP_PASSWORD` empty; `MAIL_FROM_ADDRESS` empty or not an address; `PUBLIC_BASE_URL` not
`https://` (a link built from it carries a token). Each sentence names the variable, never its value.

## Alternatives

- **A vendor's HTTP API through its SDK.** It buys idempotency keys and richer error bodies; neither
  is needed at this volume. It costs a dependency, ties the adapter to one vendor's JSON, and leaves
  dev and CI testing a fake of that vendor rather than the adapter that runs in production. The
  reasonable choice if volume ever makes idempotent sends or webhooks matter.
- **Run our own MTA on the box.** Rejected: deliverability is a reputation problem, a fresh VDS IP has
  none, and outbound port 25 is often blocked. It would also add a long-running daemon to a one-box
  deploy for three messages.
- **Send inside the request.** Rejected: a provider outage would become a failed registration, the
  request would wait on a network round trip to a third party, and the token would be minted where
  the response is built.
- **Mint the token in the route and pass it in the task.** Rejected (decision 3): a credential in a
  broker message with no TTL policy for that purpose.
- **Send, then commit.** Rejected (decision 3's table): it mails links that may never work.
- **Celery `autoretry_for` with a `countdown`.** Rejected: two retry mechanisms multiply, and on Redis
  a delayed message is redelivered every `visibility_timeout` (ADR-0014 §6).
- **The default `celery` queue.** Simpler, and the purge already rides it. Rejected (decision 2, and
  ADR-0005's amendment): the queue that a waiting person depends on should be separable.
- **Route mail through the guarded egress.** Rejected (decision 5): its obligations answer a caller
  choosing the host, which cannot happen here; applying them would teach that every outbound call is
  the same threat.
- **`aiosmtplib`.** A dependency to avoid a thread in a worker whose loop serves one task. Rejected.
- **A fake mail adapter selected by a setting in dev.** Rejected: dev would no longer exercise the
  production adapter, and the switch would exist in production too.
- **HTML mail.** Rejected: more surface for no gain, and a body that a provider is tempted to rewrite
  for tracking.

## Consequences

- **A new third party sees every account holder's address.** Stated to the user on the pages where
  the address is typed and in Constitution §8; named, with its retention, in this ADR's amendment at
  T44.
- **A mail outage delays a confirmation; it never breaks registration.** The request only records and
  enqueues. If the **broker** is down the enqueue fails and the request answers 503 — the row is
  durable, and a retry of the request supersedes it.
- **A lost mail is recovered by the user, not by the system.** A worker that dies after the commit and
  before the send leaves an issued row that will never be mailed; *Send it again* is the recovery. That
  is the price of *every mailed link works*, and it is stated in the copy.
- **Deliverability is now an operational concern**, with no symptom in any log: a mail in a spam
  folder looks like a registration that "doesn't work". SPF, DKIM and DMARC alignment and a real
  delivery's headers are read before the release, not assumed.
- **The box's `.env` must carry `MAIL_*` before the image that needs them starts**, or
  `check-settings` refuses and the API never starts. The release orders it.
- **A thread running `smtplib` cannot be cancelled.** The bounds are the socket timeout, the attempt
  count and the total deadline, all below Celery's limits.
- **This ADR owes an amendment**: the vendor, its retention setting and its tracking setting, at T44.
