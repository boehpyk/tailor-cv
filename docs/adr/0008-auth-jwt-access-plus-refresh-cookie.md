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
  may do that, and it must be idempotent. *(Refined in amendment (f) below: a route reading both
  credentials is a named **transfer route**; the claim is the second one, after slice 2.2's copy.)*

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
*(Closed in amendment (h) below: the channel exists, and registration no longer enumerates.)*

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

## Amendment: 2026-09-25, from the plan of slice 2.2 (`intake-saved-base-cvs`)

Slice 2.1 turned *"no route depends on both `require_user` and `require_guest_session`"* into a tested
rule — a walker over the live application's dependency graph — and named one future exception, the
2.4 claim. Slice 2.2 needs a second exception in the other direction: a saved CV (user-owned) must
reach the workspace (guest-owned) as a working copy (ADR-0022). The rule is refined rather than
abandoned.

**(f) A route answers to one credential, except a named transfer route.** A *transfer route* carries
data between the two principals. It reads both credentials, and **each credential authorizes only
its own half**: the bearer authorizes reading the source, the guest cookie owns the destination. No
code ever asks "is there a user *or* a guest?" — a transfer route needs both and uses each for one
thing, so the conflation this ADR forbids (a guest session as a point on the same scale as a login)
does not arise. Four mechanics keep it honest:

- **Order.** `require_user` is a dependency, because it never mutates anything. The source is loaded
  and authorized **first**; only then is `resolve_or_start_guest_session` called, **inside the
  handler body**. A sibling `Depends` would run even when body validation fails, so a dependency form
  would mint a guest session on a 422. A 401, a 422 or a 404 on a transfer route therefore mints no
  session and sets no cookie.
- **Enforcement.** The dependency walker cannot see a call inside a handler body — which is exactly
  why a quiet exception would pass 2.1's walker while breaking its rule. The walker is extended with
  an **AST scan of the router modules** for calls to `resolve_or_start_guest_session` and
  `read_guest_token`, and it pins the set of routes that both depend on `require_user` and touch the
  guest cookie to an explicit list. A route added with both, anywhere, turns it red. The scan also
  asserts the guest cookie's name is referenced only where it is already read, so a new reader must
  pass through one of the two scanned helpers.
- **CSRF.** The bearer is a header that only our page's JavaScript holds; a cross-site form cannot
  send it, so a transfer route cannot be driven by another site even though the guest cookie is
  `SameSite=Lax`. No `Origin` check is needed on bearer routes, and none is added. The trusted-`Origin`
  check (ADR-0021) stays where it belongs: on the endpoints that act on the refresh cookie.
- **The named exceptions.** Today the set is exactly **`POST /api/base-cvs/copies`** — copy a saved
  CV into this browser's workspace (slice 2.2). The **claim** (slice 2.4), which re-keys a guest's
  work to the user, will be the second, and it joins the list in the slice that builds it, under the
  same order and the same scan. Any other route that wants both credentials is a design question for
  an ADR, not an edit to the list.
  *(Revised in amendment (g) below: the copy is retired, the claim is the only exception, and the
  order rule is restated so it reads the same in both directions.)*

## Amendment: 2026-10-01, from the plan of slice 2.4 (`workspace-registration-cta`)

Slice 2.4 builds the claim (ADR-0025) — the second transfer route (f) named — and retires the first.
The two are mirror images, and (f)'s order rule was written for only one of them:

| | Source (read) | Destination (written) | May mint a guest session? |
|---|---|---|---|
| Copy (2.2, retired) | a saved CV — **bearer** | the guest workspace — **cookie** | yes: the destination may not exist yet |
| Claim (2.4) | the guest's work — **cookie** | the account — **bearer** | **never**: no session means nothing to move |

**(g) The exception set is exactly `{POST /api/me/guest-work/claim}`, and the order rule is stated by
its reason, not by its direction.**

- **The set.** The copy route is removed in the same slice (ADR-0022 (d)): since 2.3 it has no
  first-party caller, and a transfer route with no caller is attack surface with no user. The claim
  joins. The walker plus AST scan pins the new set, and its watched helpers gain `clear_guest_cookie`,
  because the claim also *writes* the guest cookie. The set changes in its own red-then-green commits,
  never together with the code.
- **The order.** (f)'s *"the source is authorized first"* was true of the copy only because the
  source happened to be the bearer's half. Its **reason** was that a `Depends` that mutates runs even
  on a 422. Restated so it holds both ways: **the bearer is a dependency** (`require_user` is
  stateless, mutates nothing, and runs first, so a bad bearer is a 401 before the guest session table
  is touched); **the cookie is read in the handler body**, through `read_guest_token` — never through
  `require_guest_session`, whose 401 would break the claim's idempotency, and never through
  `resolve_or_start_guest_session`; and **the destination (the user) is resolved before the source is
  looked at**, so an erased user is a 401 even when the cookie names nothing.
- **A transfer route whose source is the guest never mints.** The claim has no body and no
  parameters, so *"a 422 mints nothing"* is vacuous for it; the stronger invariant is pinned instead:
  no response of the route sets a non-empty `tc_guest`. Its error responses neither clear nor set the
  cookie; a 200 clears it iff the request carried one.
- **Idempotent, in effect.** The Consequence above says the claiming endpoint *"must be idempotent"*.
  It is, without an idempotency key: the claim deletes the guest session it consumes (ADR-0025
  decision 4), so a retry presents a cookie that names nothing and receives **200 with zero counts**
  — the same answer as a browser with no guest work.
- **CSRF** is (f)'s reasoning unchanged: the bearer is a header only our page's script holds. The
  route lives under `/api/me/`, not `/api/auth/`, so the refresh cookie (`Path=/api/auth`) never
  travels with it and ADR-0021's `Origin` check does not apply.

A third route that wants both credentials is still a design question for an ADR, not a list edit.

## Amendment: 2026-10-02, from the plan of slice 2.5 (`identity-email-verification`)

Amendment (b) conceded that registration reveals whether an address has an account *"until an email
channel exists"*, and named the one design that does not: *always 202, and email the address*. Slice
2.5 builds the channel (ADR-0026) and the design (ADR-0027).

**(h) Registration no longer reveals whether an address exists.**

- **`POST /api/auth/register` answers 202 with an empty body and no `Set-Cookie`** for every address
  that parses and every password the policy accepts. It creates a *pending registration*, not a
  `User`, and never looks the address up: the worker decides whether to mail a confirmation link or
  the account-exists notice, and the requester cannot tell which (ADR-0027 decision 2). The response
  body, its headers and its status are byte-identical for a registered and an unknown address; the
  request's use case makes zero `UserRepository` calls; timing is measured on the production image.
- **409 `email_already_registered` moves to confirmation**, where the only person who can receive it
  holds a token mailed to that address (ADR-0027 decision 3). It is no longer a response of
  `register`.
- **Registration does not sign in, and neither does confirmation.** The refresh cookie is set by
  `login` alone. This changes 2.1's *"register, and you are signed in"*; the cookie's attributes,
  rotation and the access token are untouched.
- **(b)'s per-IP limit stays (5/h) and a per-address limit joins it (3/h)**, keyed on the HMAC of the
  normalized address (ADR-0021 §5), checked after the address parses and before the password policy
  runs. Both fail closed, by (e)'s rule (ADR-0021's amendment). A per-address limit answers 429 for a
  registered and an unknown address alike, so it leaks nothing.
- **Login is unchanged in what it reveals**: (b)'s decoy and its timing equality stand, re-measured
  in 2.5 because login gains a credential re-check on its matching path (ADR-0028), not on the
  unknown-email or wrong-password paths.
- **What (b) conceded is now closed, with one residue stated elsewhere**: an attacker can register an
  address they do not own and, if its owner clicks the confirmation, create an account in the owner's
  name with the attacker's password. Confirmation not signing in, and the reset, are what make that
  harmless (ADR-0027 decision 6, ADR-0028).

## Amendment: 2026-10-10, from the plan of slice 4.1 (`identity-user-roles`)

Slice 4.1 adds the first route that asks for a fact about the user beyond the bearer: every
`/api/admin/*` route requires the admin role (ADR-0032).

**(i) A route may require the bearer and a role. The role is not a credential and not a claim.**

- **The token is unchanged.** Still exactly five claims (`iss`, `aud`, `sub`, `iat`, `exp`); no
  `role`. The role is read from `identity_user` on every admin request, so a grant or a revoke takes
  effect on the next request and needs no token revocation.
- **(f)'s "one credential per route" holds.** An admin route answers to the bearer alone. The role
  is a fact the server looks up for the user the bearer names, not a second thing the client
  presents. Admin routes read no guest cookie, and the transfer-route set is unchanged.
- **The order is the bearer's first.** `require_admin` depends on `require_user`: a missing or
  expired bearer is 401 (so the client's refresh-on-401 still works), a deleted user is 401
  `not_signed_in`, and only then is the role read. A signed-in user without the role gets 404,
  byte-identical to an unmatched route (ADR-0032 decision 4).
- **No cookie, no `Origin` check.** Admin routes are bearer-only, so ADR-0021's `Origin` set stays
  eight.
