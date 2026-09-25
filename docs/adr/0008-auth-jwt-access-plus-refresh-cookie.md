# ADR-0008: Auth — short-lived JWT access token plus a rotating refresh cookie

- **Status:** Accepted
- **Date:** 2026-09-04

## Context

Registration is **optional** (US-6, FR-6): the whole product works for a guest. Auth therefore is not
a gate in front of the app, it is an *upgrade* — which makes the interesting design question not "how
do we log people in" but "how do requests carry identity when identity is optional".

The frontend is a separate origin in development (Vite on `:5173`, API behind nginx on `:8080`) and a
same-origin path in production (`/api` behind one nginx). Anything chosen must work in both without a
second code path.

PRD §9 names "JWT/Session" without deciding. This ADR decides.

## Decision

**A short-lived JWT access token (≈15 min) held in memory by the React app, plus a long-lived
rotating refresh token in an `HttpOnly`, `Secure`, `SameSite=Lax` cookie.** *(Corrected in the
Amendment below: `SameSite=Strict`, `Path=/api/auth`, host-only.)*

- The access token is **never** written to `localStorage`. A token in `localStorage` is readable by
  any script that gets onto the page, and this app renders LLM-generated content into a rich text
  editor — the exact situation where an XSS assumption should not be load-bearing.
- The refresh token is opaque, stored server-side (hashed), **rotated on every use**, and a re-use of
  an already-rotated token invalidates the whole family. Detecting replay is the point of rotation,
  not the token length.
- **Guest sessions use a separate, non-auth cookie** carrying an opaque session id. It grants access
  to that session's own workspace and nothing else. It is not a JWT, it is not a degraded login, and
  the code must never treat "has a guest session" and "is authenticated" as points on one scale — that
  conflation is how a guest ends up reading someone's saved CV.
- Passwords are hashed with **argon2id**. Registration and login answer in a way that does not reveal
  whether an address exists. *(Corrected in the Amendment below: login does not; registration does,
  until an email channel exists.)*
- Every request is authorized against the resource it names. **Owning a session id is not authority
  over an object that references it** — check the link, every time, in the use case.

## Alternatives

- **Server-side sessions in Redis for everything.** Genuinely simpler, and it makes logout instant.
  Rejected narrowly: it makes Redis load-bearing for *login itself* (Redis down = nobody can
  authenticate), and stateless access tokens keep the API honest about not reaching for shared state
  mid-request. Reconsider if session invalidation latency ever becomes a real complaint.
- **Access token in `localStorage`.** The most common tutorial answer and the one this app can least
  afford. Rejected — see above.
- **Long-lived access tokens with no refresh.** Rejected: revocation becomes impossible without a
  denylist, which is the state you were trying to avoid, arrived at accidentally.
- **A third-party identity provider (OAuth-only).** Rejected for launch: the audience is a friend
  group, and email/password is the smallest thing that works. A Google adapter is a later slice, and
  the `User` aggregate should not assume a password is the only credential.

## Consequences

- **Rotating refresh tokens make concurrent requests interesting.** Two tabs refreshing at once can
  race and one legitimately looks like a replay. Handle it explicitly — a short grace window on the
  previous token, or a single-flight refresh in the client — and write the test. Discovering this in
  production presents as "it randomly logs me out", the least debuggable bug report there is.
- The React app needs exactly **one** place that knows about auth state and one interceptor that
  refreshes on 401 and retries once. Anything more distributed becomes a maze.
- Rotating `JWT_SIGNING_KEY` logs everyone out. That is intended, and it is the break-glass control.
  *(Corrected in the Amendment below: it does not, and the break-glass is `revoke-logins --all`.)*
- Phase 2.4's guest→registered claim flow crosses this boundary: a request that arrives with **both**
  a guest cookie and an access token is the moment work is re-keyed to the user. Exactly one endpoint
  may do that, and it must be idempotent.

## Amendment: 2026-09-23, from the plan of slice 2.1 (`identity-register-and-login`)

This records an amendment rather than a rewrite. Slice 2.1 is the first code to implement this ADR,
and planning it against the real deployment found five sentences above that were either wrong or
undecided. They keep their original text and carry an inline *(Corrected …)* pointer.

**(a) The refresh cookie is `SameSite=Strict`, `Path=/api/auth`, and host-only (no `Domain`).**
`cv.samolit.com` shares its registrable domain with every other `*.samolit.com` app behind the same
Traefik on the same box. A sibling subdomain is *same-site*, so `SameSite=Lax` does not stop a
request from it — an XSS on a sibling would be a CSRF on us. `Strict` costs nothing here: the cookie
is only ever needed by a `fetch` from our own page to `/api/auth/refresh`, never by a top-level
navigation. `Path=/api/auth` keeps the credential off every other request, and host-only keeps a
sibling from ever *receiving* it. The layer that actually stops a same-site sibling *sending* a
request is a trusted-`Origin` check on the four cookie-touching endpoints (ADR-0021).

**(b) Login does not reveal whether an address exists. Registration does, until an email channel
exists.** Every registration design without an out-of-band channel leaks existence — immediately (a
duplicate cannot be logged in, so it answers differently) or one step later (an always-202
registration followed by a login that succeeds or fails). Only "always 202 and email the address"
does not leak, and that needs an email adapter, a sender domain, deliverability and a verification
token table — a slice of its own, planned for Phase 2 after 2.4. Until then a duplicate registration
answers **409 `email_already_registered`**, registration is rate-limited to 5/h per IP, and the
residual risk (someone can learn whether an address has an account here) is stated rather than
pretended away. Login keeps the promise: an unknown email and a wrong password get byte-identical
responses, and an unknown email is verified against a decoy hash so both cost the same time.

**(c) The race is handled by both mechanisms this ADR offered, because each covers a race the other
cannot.** A single-flight refresh in the client covers races *within* a tab (two components mounting,
`<StrictMode>` running the boot effect twice). A **10-second server grace** on the immediate
predecessor covers races *across* tabs and devices, which no in-memory promise can see; inside the
grace the answer is **409 `refresh_in_progress`** with no state change — never a second current
token. The model is ADR-0020.

**(d) Rotating `JWT_SIGNING_KEY` does not log anyone out.** Refresh tokens are opaque random values
looked up by hash, not signed values, so a new key invalidates every *access* token and the next
silent refresh mints one under the new key. That is the right behaviour for a key rotation, and the
sentence above claiming otherwise was never true of the design it sat in. **The break-glass that logs
everyone out is `python -m tailorcraft.cli revoke-logins --all`**, which deletes every login.

**(e) The login and registration rate limiters fail closed; refresh has no limiter.** The codebase's
rule is *fail open when the cost is ours and bounded; fail closed when the cost is money or somebody
else's*, and an unlimited password guesser spends somebody else's account. So a Redis outage stops
*new* logins. It never logs anybody *out*: refresh, logout and `/me` do not touch Redis. That narrows
the "Redis down = nobody can authenticate" objection in *Alternatives* to "nobody can start a login",
which is the half worth accepting.
