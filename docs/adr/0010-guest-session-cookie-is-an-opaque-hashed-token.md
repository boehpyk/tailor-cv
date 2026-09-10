# ADR-0010: The guest session cookie is an opaque token, stored hashed

- **Status:** Accepted
- **Date:** 2026-09-07
- **Extends:** ADR-0006 (which decided *that* a `GuestSession` exists), ADR-0008 (which decided
  authentication for *registered* users). Supersedes nothing.

## Context

ADR-0006 decided that guest-owned rows reference an expiring `GuestSession`. It did not say what the
browser actually carries, and slice 1.1 is the first code that needs an answer.

The obvious implementation is to put the session's UUID primary key in a cookie and look rows up by
it. It works, it is one column, and it is what most tutorials do. Two things make it the wrong choice
here.

**A guest session id is a credential.** Constitution §8 and ADR-0008 both state that owning a session
id is not authority over an object that references it — the *link* is checked in the use case, every
time. But that rule governs authorization *after* the session is identified. Identifying the session
in the first place is authentication, and whatever the cookie holds is the entire secret. There is no
password behind it.

**A value that is both the credential and the database key ends up somewhere it shouldn't.** Primary
keys are the values that get logged, put in error messages, printed in a debug view, pasted into a
support chat, and appended to URLs. Every one of those is harmless for a key and a compromise for a
credential. Keeping them the same value means every future logging decision is also a security
decision, made by someone who is thinking about debugging.

There is a specific aggravating factor: this project uses **UUIDv7**, whose leading 48 bits are the
creation timestamp in milliseconds. A UUIDv7 credential leaks when it was minted and is not uniformly
random across its length — neither is fatal, and neither is a property you want in a bearer token.

## Decision

**1. The cookie carries an opaque random token.** `secrets.token_urlsafe(32)` — 256 bits from the
OS CSPRNG. It is not derived from the session id, the clock, or anything else.

**2. The database stores only `sha256(token)`**, in `identity_guest_session.token_hash`
(`CHAR(64)`, unique). Lookup hashes the presented token and matches on that column. The raw token
exists in the browser and in the request; it is never written down on our side.

**3. Unsalted SHA-256 is correct here, and a slow KDF would be wrong.** Argon2 and bcrypt exist to
make *guessing* expensive for **low-entropy** secrets — passwords, which people reuse and choose
badly. There is no dictionary to attack a 256-bit random token with, so a slow hash would buy nothing
and cost a KDF on every single request. This reasoning gets a comment at the hashing call site,
because "why isn't this argon2" is exactly the review question a careful reader should ask, and the
answer should be there rather than rediscovered.

**4. Cookie attributes are fixed:** name `tc_guest`, `HttpOnly`, `SameSite=Lax`, `Path=/`,
`Max-Age=86400` (matching `guest_retention_hours`, so the cookie and the data expire together), and
`Secure` whenever `APP_ENV=production`.

**5. `POST` and `GET` treat a bad cookie differently, on purpose.** A missing, unknown or expired
cookie on `POST /api/base-cvs` **mints a new session** and the upload succeeds — a first-time visitor
has no cookie, and that is the normal case, not an error. The same cookie on either `GET` returns
**401 `guest_session_expired`**, because there is nothing to show and inventing an empty session would
tell the user their work is gone by pretending it never existed.

## Alternatives

- **The session UUID as the cookie value.** The tempting one, rejected above: it makes the primary key
  a bearer credential, and with UUIDv7 a timestamped one.
- **A signed cookie (JWT or `itsdangerous`) with no database row.** Rejected: a guest session must be
  *revocable and expirable server-side*, because the 24-hour purge (ADR-0006) deletes the data and a
  self-contained token would still validate against data that no longer exists. A row that can be
  deleted is the point.
- **Argon2/bcrypt over the token.** Rejected above — a KDF per request to protect a secret with no
  dictionary.
- **Reusing ADR-0008's JWT machinery for guests.** Rejected, and worth stating plainly: **a guest
  session is not a weak login.** Sharing the mechanism invites sharing the semantics, and the first
  time someone writes `if current_user or guest_session` the distinction is gone. Phase 2.4's
  guest→registered claim flow is a deliberate hand-off between two different things, not a promotion
  within one.
- **A salted hash.** Rejected as ceremony: salts defend against precomputation across many low-entropy
  secrets. There is no rainbow table for 256-bit random values.

## Consequences

- **One SHA-256 per authenticated request.** Microseconds, against an indexed unique-column lookup
  that dominates it.
- **A lost cookie is unrecoverable, by design.** There is no reset path, because there is no identity
  to prove. The UI must say what the 24-hour window means before the user relies on it (ADR-0006 §5),
  and registration is the answer to "I don't want to lose this".
- **`token_hash` is not PII and must not become a join key to one in a log.** Logging the
  `guest_session_id` is fine; logging it in the same line as a client IP or a filename is the pairing
  that links a person to a CV. Named here because the rule is easy to keep and easy to forget.
- Phase 2.4's claim flow re-keys guest work to a user **before** the session expires, and it must
  invalidate the guest token at the same moment. Otherwise a stale cookie keeps read access to work
  that now belongs to an account.
- If guest sessions ever need to be listed or revoked by an operator, the token hash is what they will
  have to work with — the raw token is genuinely gone. That is the intended trade.
