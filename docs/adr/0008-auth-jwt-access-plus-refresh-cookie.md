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
rotating refresh token in an `HttpOnly`, `Secure`, `SameSite=Lax` cookie.**

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
  whether an address exists.
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
- Phase 2.4's guest→registered claim flow crosses this boundary: a request that arrives with **both**
  a guest cookie and an access token is the moment work is re-keyed to the user. Exactly one endpoint
  may do that, and it must be idempotent.
