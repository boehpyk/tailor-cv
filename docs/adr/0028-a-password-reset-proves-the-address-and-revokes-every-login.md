# ADR-0028: A password reset proves the address and revokes every login

- **Status:** Accepted
- **Date:** 2026-10-02
- **Relates to:** ADR-0020 (revocation is deletion — **amended**: a reset deletes every login, and
  expired logins are swept), ADR-0021 (argon2 on a bounded executor, the `Origin`-checked surface and
  the limiters — **amended**), ADR-0022 (a sum type with no methods, the shape `ResetTarget` copies),
  ADR-0026 (the channel; the token minted in the worker), ADR-0027 (the token design, the request path
  with no branch, and the pre-hijack residue a reset recovers), ADR-0018 (the sweep — **amended**).
  Supersedes nothing.

## Context

Since slice 2.2 a registered user keeps saved CVs and, since 2.3, an application history. A forgotten
password locked all of it away for good: there was no recovery, and 2.2's *delete account* asks for the
very password that was forgotten. Slice 2.5's mail channel makes a reset possible, and three forces
shape it:

1. **A reset is the remedy for a compromised password**, so it must evict whoever holds the old one —
   every device, not only the one being used.
2. **A reset request must not reveal whether an address has an account** — the same promise
   registration now keeps (ADR-0008 (h)).
3. **A race 2.1's design did not have.** `LogIn` reads the user, verifies the password on the argon2
   executor (≈ 50 ms), then inserts a `Login`. A reset that commits inside those 50 ms would let a
   login **with the old password** complete *after* the reset, and its `Login` would survive it — the
   exact attacker the reset exists to evict. Worse, a login whose hash needed upgrading would save a
   hash of the **old** password over the new one.

## Decision

**1. `PasswordReset` is an aggregate with two shapes and no third.** It is created **addressed** —
`AddressedReset(email)`, holding only what was typed — and becomes **issued** — `IssuedReset(user_id)`
with a token hash — when the worker resolves the address to an account. On issue **the address is
dropped**: the account row holds it, and the reset keeps nothing it does not need. `ResetTarget =
AddressedReset | IssuedReset` is a sum type with no methods (ADR-0022's `Owner` shape), stored as two
nullable columns under `ck_identity_password_reset_exactly_one_target`. A reset ends by **deletion** —
on use, on expiry, on a newer issued reset for the same account (supersede), or with the account
(cascade). It lives 60 minutes by default (`PASSWORD_RESET_TTL_MINUTES`, bounded 10–240).

**2. The request path has no branch, and no hash.** `POST /api/auth/password-reset`: `Origin` check →
per-IP limiter → parse the address → per-address limiter → insert an addressed reset (committed) →
enqueue its id → **202, empty**. No `UserRepository`, no hasher: the request is one insert and one
enqueue for every address. The **worker** asks `find_by_email`: no account → delete the reset, send
nothing; an account → mint a token, issue (which also deletes the account's other resets), commit,
then send *Reset your password* (ADR-0026 decision 3). The token, its storage and its fragment-borne
link are ADR-0027 decision 5's, under `/reset-password/confirm#token=…`.

**3. Completing a reset replaces the hash and deletes every login and every reset of the account, in
one transaction.** `ResetPassword(token_hash, password)` finds the reset, locks the **user** `FOR
UPDATE`, re-finds the reset by hash `FOR UPDATE`, refuses it if expired (deleted, committed, refused),
checks the password policy against the account's address, hashes once, deletes every `Login` of the
user, calls `user.reset_password(hash, at, logins_revoked=n)` — which records
`PasswordChangedByReset(user_id, logins_revoked)` on the aggregate — saves, deletes the user's resets,
and commits. Revocation is deletion (ADR-0020 §5); there is no flag.

**4. Completing a reset signs nobody in, including this tab.** The user logs in with the new
password; `LogIn` stays the one code path for *who is signed in*. Access tokens already issued live out
their ≤ 15 minutes, as after logout and after `revoke-logins --all`: a per-request check against the
password's age would put a database read on every bearer request to shorten a window the system
already accepts.

**5. `LogIn` and `DeleteOwnAccount` re-check the credential under a lock, after verifying and before
any write:**

```sql
SELECT 1 FROM identity_user WHERE id = :u AND password_hash = :seen FOR SHARE
```

- A reset committed first → under READ COMMITTED, `FOR SHARE` re-evaluates the predicate on the latest
  row version, which no longer matches → `InvalidCredentials` (403 for a deletion). No `Login` is
  created and no rehash is saved.
- The login's `FOR SHARE` taken first → the reset's `FOR UPDATE` on the user waits for the login to
  commit, then deletes the `Login` it created.

It compares the **hash**, not a timestamp: timestamps are whole-second here, and a reset in the same
second as the previous change would compare equal, whereas argon2's salt makes two hashes of one
password differ. Only the *matching* path takes the re-check, so the unknown-email and wrong-password
paths are unchanged and ADR-0021's timing equality is untouched (re-measured, not assumed).

**6. The lock order: the user before a reset, everywhere.**

| Actor | Takes, in order |
|---|---|
| Reset completion | reset read (no lock) → **user `FOR UPDATE`** → reset `FOR UPDATE` → `DELETE` logins, resets |
| `LogIn` (matching) | user `FOR SHARE` → `INSERT` login (its FK takes the user `FOR KEY SHARE`) |
| `DeleteOwnAccount` | user `FOR SHARE` → account erasure's user `FOR UPDATE` (same transaction) → cascades |
| Account erasure | **user `FOR UPDATE`** → cascades (logins, issued resets) → `DELETE` resets and pending registrations by address |
| Reset issue (worker) | `UPDATE` the reset's target (its FK takes the user `FOR KEY SHARE`) → `DELETE` the user's other resets |
| Token sweep | per table, `DELETE … WHERE expires_at <= :t` in batches, no user lock |

Everything that takes both the user and a reset takes the user first, so erasure and reset completion
cannot deadlock. A sweep that reaches a reset completion has locked waits; a completion that deleted
it first leaves the sweep nothing to do.

**7. No limiter on completion, and argon2 at most once per valid token.** A token is 256 bits;
guessing is not a threat a limiter changes, and garbage costs one indexed lookup (a malformed token,
none). A policy refusal hashes nothing and keeps the token, so the user can choose another password.
Redis down therefore never blocks finishing a reset that was already mailed.

**8. A reset proves the address.** For an account created before 2.5 (ADR-0027 decision 8), the first
reset is the first proof that its holder reads mail there; nothing records it, because nothing reads
the difference.

## Alternatives

- **Check `password_updated_at` against the token's `iat` on every bearer request.** It would end
  access tokens at the reset instead of within 15 minutes. Rejected: a read on every authenticated
  request to shorten a window logout and the break-glass already accept.
- **Compare `password_updated_at` instead of the hash in the re-check.** Rejected: whole-second
  timestamps make a same-second reset invisible.
- **Lock the user `FOR UPDATE` in `LogIn`.** Correct, and it would serialize a user's concurrent
  logins on a lock that protects nothing they share. `FOR SHARE` is the weakest lock that conflicts
  with the reset's.
- **`SERIALIZABLE` isolation for login and reset.** Rejected: a serialization failure is a retry loop
  the routes do not have, to buy what one row lock buys.
- **Keep the current device's login after a reset.** Rejected: a reset done from an attacker's
  session would keep it alive, and *"everyone is signed out"* is a sentence a user can trust.
- **The reset signs the user in.** Rejected for the same reason as confirmation (ADR-0027 decision 6),
  and because it would be a second path that mints a login.
- **Keep the typed address on an issued reset.** Rejected: data minimisation; the account row holds it.
- **Mail a *"no account here"* notice to an unknown address.** Not now: it would make the reset form
  a way to send a message with no useful action to any address. The requester sees the same *"if an
  account exists, we've sent a link"* screen either way.
- **A *"your password was changed"* mail after a reset.** Not now: the person resetting controls the
  mailbox it would go to. It belongs with a password change while signed in, a later slice on this
  channel.
- **Security questions, or account lockout after failed attempts.** Rejected: the first is a second,
  weaker password; the second is a denial of service anyone can perform (ADR-0021).

## Consequences

- **A reset is a revocation of every device**, the second in this codebase after `revoke-logins
  --all`, and the first a user can trigger. The `PasswordChangedByReset` event carries the count.
- **`LogIn` gains a database round trip and a row lock on its matching path.** Login p95 is
  re-measured on the production image, and so is the unknown-versus-wrong-password timing.
- **`DeleteOwnAccount` takes the same re-check**, so an erasure confirmed with a password that a reset
  has just replaced is refused rather than performed.
- **Access tokens outlive a reset by up to 15 minutes**, as they outlive logout. Stated, and asserted
  by a test rather than discovered.
- **The lock order is a rule for future code**: anything that will hold the user and a reset takes
  the user first. Each race above is staged on two real connections with the overlap read from
  `pg_stat_activity`.
- **2.2's self-deletion is now recoverable** for a user who forgot the password: reset first.
