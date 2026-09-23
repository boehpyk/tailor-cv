# ADR-0020: A login is a refresh-token family, and revocation is deletion

- **Status:** Accepted
- **Date:** 2026-09-23
- **Relates to:** ADR-0008 (rotation with family revocation — this ADR chooses the model it left
  open), ADR-0010 §3 (why an opaque 256-bit token is hashed with SHA-256, not a KDF), ADR-0015
  (optimistic concurrency as an explicit `WHERE version = :v`), ADR-0018 (no audit table of
  deletions). Supersedes nothing.

## Context

ADR-0008 decided *that* refresh tokens rotate on every use and that re-using a rotated token
invalidates "the whole family". It did not decide what a family *is* in the model, how old a token
can be and still be recognised, what "invalidate" means on disk, or how the race it named (two tabs
refreshing at once) is answered. Slice 2.1 has to decide all four before it writes a table.

Three forces:

1. **Reuse detection is the point of rotation.** A token stolen three rotations ago must be
   recognised as *ours and retired*, not as garbage — otherwise rotation only detects the dullest
   theft.
2. **An honest second tab looks exactly like a thief** for a few hundred milliseconds. ADR-0008
   warned that discovering this in production presents as "it randomly logs me out".
3. **The product's premise is keeping less.** A row whose only job is to say "this is dead" needs a
   retention policy of its own; ADR-0018 declined exactly that shape for purge runs.

## Decision

**1. A `Login` aggregate *is* the family.** One row per successful authentication on one device:
the current token's hash, a **generation** (1 at login, +1 per rotation), `rotated_at`, an absolute
`expires_at`, and a `version` for optimistic concurrency. Its invariant — *exactly one current token;
generations advance by one; an expired login never rotates* — is held by one row, so it is unit-testable
with no I/O. `Login` is **not** inside `User`: they change at wildly different rates, and a rotation
never changes the user, so putting logins in the user aggregate would serialise a user's two tabs on
a lock that protects nothing.

**2. Retired hashes live in an append-only lookup table**, `identity_retired_refresh_token`
(`token_hash` PK, `login_id`, `generation`, `retired_at`), kept for the life of the login. It is
written and read with Core and never loaded as a collection: it answers "whose token was this, and
which generation?" for **any** past generation.

**3. The race grace is 10 seconds and a domain constant.** A retired token that is the *immediate
predecessor* of the current one (`generation == current − 1`), presented within 10 s of the rotation
(inclusive), is `RACED`: the answer is **409 `refresh_in_progress`**, nothing changes, and no second
current token is ever minted — the server holds only hashes and could not hand the winner's token to
the loser anyway. Anything else retired is `REUSED`. It is a constant, not a setting, because it is a
property of what a race *is*, and this codebase does not give a control a knob. The client adds a
single-flight refresh per tab (ADR-0008 amendment (c)); neither mechanism alone covers both races.

**4. Two concurrent rotations of the same current token are decided by the database.** The
repository writes `UPDATE … WHERE id = :id AND version = :loaded` and inserts the retired row; zero
rows updated, or a primary-key collision on the retired hash, is `LoginConcurrentlyRotated`, answered
as 409. Not SQLAlchemy's `version_id_col`, which raises from inside a flush and expires the identity
map (the ADR-0015 precedent and its reason).

**5. Revocation is `DELETE`.** Logout, reuse detection and expiry-on-sight delete the `Login` row;
its retired hashes go by `ON DELETE CASCADE`. There is no `revoked` flag and no `revoked_at`. After a
revocation any token of that login is simply unknown — 401 `not_signed_in` — which is the correct
answer. The reuse itself is reported once, at detection: a `RefreshTokenReuseDetected` event and a
warning log line with ids and generations, never a hash.

**6. The lifetime is absolute: 30 days from login, never extended by rotation.** A sliding lifetime
would keep a stolen-and-used token alive indefinitely. The cookie's `Max-Age` is the *remaining*
lifetime, so a rotation on day 29 does not reset it.

**7. Refresh tokens are `secrets.token_urlsafe(32)` stored as SHA-256**, by ADR-0010 §3's argument:
entropy decides the hash. A 256-bit CSPRNG value cannot be guessed, so a KDF's cost would be pure
waste on a path that runs every 15 minutes per tab. The application layer only ever sees the hash
(`TokenHash`); the route mints and hashes.

## Alternatives

- **One row per token, grouped by `family_id`, a `revoked` flag on each.** Rejected: "the family" is
  then an emergent `GROUP BY`, not an aggregate, and no single row can say "this family has exactly
  one current token" — the invariant would live in a query.
- **One family row holding the current and the previous hash only.** Rejected: it recognises reuse of
  the immediate predecessor and nothing older, which throws away most of what rotation is for.
- **A `revoked` state kept "for forensics".** Rejected: a table nobody reads, holding user ids and
  instants, that needs its own retention policy.
- **A sliding lifetime.** Rejected above (6).
- **`navigator.locks` as a cross-tab mutex instead of a server grace.** It works within one browser,
  but a second *device* cannot share it, so the server must handle the race anyway — and it teaches
  the reader to trust a client to protect a server invariant.
- **Issuing the loser of a race a fresh token.** Rejected: it breaks "exactly one current token", the
  one invariant `Login` exists for.

## Consequences

- **A rotation whose response is lost logs the user out** once the grace has passed: the browser never
  received the new cookie, so its next refresh presents a retired token. Accepted — it is the price of
  rotation, and the alternative (not revoking) is the thing rotation exists to prevent. The user
  re-enters a password and loses nothing.
- **Expired logins that are never presented again accumulate**, roughly 96 retired hashes per tab per
  day of use. There is no sweep yet; deletion on sight covers every login that comes back. Owner
  `api-dev`; trigger: `identity_retired_refresh_token` above 100 k rows, or slice 2.2 (which adds
  account deletion), whichever comes first — a beat sweep reporting under ADR-0019's `jobs` rule.
- **Logging in again in the same browser does not revoke the previous login**: its cookie is
  overwritten, so it becomes unreachable and lives until its absolute expiry. Reading the old cookie to
  delete it would make login depend on a second credential for housekeeping.
- **"Revoked for reuse" and "never existed" are indistinguishable on the next presentation.** Nothing
  needs the difference; the reuse was reported when it happened.
- **The break-glass is `revoke-logins --all`**, a `DELETE` of every login. Rotating the JWT key is not
  one (ADR-0008 amendment (d)).
