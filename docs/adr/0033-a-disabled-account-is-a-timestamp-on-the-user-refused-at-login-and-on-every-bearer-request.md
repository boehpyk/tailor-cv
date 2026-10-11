# ADR-0033: A disabled account is a timestamp on the user; its logins are deleted in the same transaction, and login and every bearer request refuse it

- **Status:** Accepted
- **Date:** 2026-10-11
- **Relates to:** ADR-0008 (auth — **amended** the same day: amendment (j), the bearer reads one
  fact from the user row), ADR-0020 (revocation is deletion — note added), ADR-0024 (read models —
  amendment (b), the user directory), ADR-0028 (a reset revokes every login — note added), ADR-0032
  (the admin firewall — note added). Supersedes nothing.

## Context

Slice 4.2 (`admin-users`) gives the admin a user directory and three actions on an account: disable,
enable, erase. Erase already exists as the operator's `erase-account` (`EraseAccount`, ADR-0006
amendment); the new fact is **disabled**.

The owner's Phase 4 decisions fix the frame: a disabled user cannot log in, its logins are revoked,
its data is kept, a password reset does not re-enable it, and its open tab is sent to `/` with a
message. Three facts from the code decide the details:

- **Revocation is deletion** (ADR-0020). Once a disable deletes the logins, a refresh cannot tell
  "disabled" from "expired": both find no row.
- **The bearer is stateless.** An access token lives 15 minutes with five claims, so deleting the
  logins alone leaves the open tab working — paid tailoring runs included — until it expires, and
  the user then learns nothing about why.
- **Lock order is already a rule** (ADR-0028): anything holding the user and a login or reset takes
  the user row first. `LogIn` re-checks the credential under `FOR SHARE` on the user row before it
  inserts its `Login`.

## Decision

1. **`identity_user.disabled_at TIMESTAMPTZ NULL`** on `User` (whole-second, from the `Clock`).
   `None` is active. `disable(at, logins_revoked)` records `UserDisabled` with the count;
   `enable(at)` records `UserEnabled`. Disabling a disabled account, or enabling an active one,
   records nothing.
2. **Disabling takes the user row `FOR UPDATE`, then deletes every `Login`** (ADR-0020's revocation),
   in one transaction. **Enabling restores the right to log in and nothing else**: no session comes
   back.
3. **`LogIn` refuses a disabled account only after a correct password**, reading the state with a
   fresh statement while its `FOR SHARE` re-check holds the user row: 403 `account_disabled`, no
   cookie, nothing committed. A wrong password or an unknown email stays `invalid_credentials`, so
   2.1's timing equality holds by construction, and "disabled" is told only to whoever holds the
   password.
4. **Every bearer request reads the disabled bit after the signature check** (ADR-0008 (j)):
   `UserRepository.is_disabled` — one primary-key read, `False` for a missing row so each route's
   existing "user gone" handling is unchanged — and a disabled account gets 403 `account_disabled`.
   The open tab learns on its next request, and the client signs out with that reason and routes to
   `/` with a notice. A bad bearer is still refused before any database read.
5. **A password reset does not re-enable.** The reset mail is still sent, a reset still works and
   still revokes logins; the account stays disabled. Pending resets are kept on disable.
6. **Admin actions lock only the target, never re-check the actor's role** (answers ADR-0032's open
   question). The window is one request, opened only by the operator's own `revoke-role`; locking
   the actor too would put two user rows in one transaction in a request-chosen order, deadlock-prone
   when two admins act on each other.
7. **Disable and enable are admin-only, through `/api/admin/users/{id}/{disable,enable}`**, behind
   ADR-0032's firewall. Erase from `/admin` is `EraseAccount`, unchanged, and answers 200 with its
   report. There is no CLI for disable or enable; if the only admin disables itself, recovery is
   `grant-role` on another account, which then enables the first.

## Alternatives

- **A status enum (`active | disabled`).** A second representation of one bit, and it loses *since
  when*. Rejected for the nullable timestamp.
- **Keep the logins and refuse at refresh.** Rejected: contradicts "logins are revoked", and an
  enable would silently resume every old session.
- **Delete the logins and nothing more** (the smallest option). The tab keeps working up to 15
  minutes, paid runs included, then fails as `expired` with no reason given. Rejected by the owner
  (spec OQ-1 = B): the "sent to `/` with a message" decision would be unmet.
- **An access-token denylist** (a `jti` in Redis). Rejected: ADR-0020 chose not to track tokens, and
  it would make Redis load-bearing for every request, where today it is not.
- **401 for a disabled account.** Rejected: the client's refresh-on-401 would spend a call and end in
  `expired`, the wrong reason. The credential is valid; the account may not use it — a 403.
- **Widen `confirm_credential_unchanged` to return an enum** instead of a second read in `LogIn`.
  Rejected: it rewrites a port method that seven test files and two fakes depend on as a `bool`, for
  one primary-key read beside a ~50 ms argon2 verify.

## Consequences

- **Easy:** a disable takes effect on the next request of every tab; enable is trivially safe; the
  token still has five claims and Redis is still untouched by authentication.
- **Cost:** one primary-key read on **every** bearer request (budget ≤ +2 ms p95, measured on the
  production image). If it ever fails the budget, look at the query, never add a cache: a cached
  "enabled" is the bug this column exists to prevent.
- **A database blip now fails every bearer route with 503**, not only those that query — in practice
  the same set, since every `/api/me/*` handler already reads Postgres.
- **A disabled user cannot delete its own account** (the bearer is refused before the handler).
  Erasure stays available through the admin and the operator CLI.
- **Watch:** a cache placed in front of `is_disabled`; a new authentication path (another token kind,
  a WebSocket) that skips `require_user`; and a migration downgrade, which refuses while any account
  is disabled, because dropping the column would silently re-enable them.
