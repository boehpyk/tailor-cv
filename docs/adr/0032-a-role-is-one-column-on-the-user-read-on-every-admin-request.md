# ADR-0032: A role is one column on the user, read on every admin request; the admin surface answers an unmatched route's 404

- **Status:** Accepted
- **Date:** 2026-10-10
- **Relates to:** ADR-0008 (auth — **amended** the same day: amendment (i), a route may require the
  bearer and a role), ADR-0020 (logins — a demotion revokes none), ADR-0007 (persistence), ADR-0021
  (the `Origin` set — **not** widened, admin routes are bearer-only), ADR-0022 (owners — not
  touched). Supersedes nothing.

## Context

Phase 4 adds an operator area. Slice 4.1 (`identity-user-roles`) builds only the role and the
firewall in front of `/api/admin/*`, with no admin features yet. It is the first time a route asks
for a fact about the user beyond the bearer.

The owner's Phase 4 decisions fix the frame: an admin is an ordinary `User` who signs in through the
existing `/login`; one role per user; the role is read from the database on every admin request;
roles change by CLI only; a non-admin gets 404, an anonymous caller 401; an admin may erase any
admin, itself included.

Three facts from the code decide the details:

- The access token carries exactly five claims (`iss`, `aud`, `sub`, `iat`, `exp`) and lives 15
  minutes. A role inside it would outlive a demotion by up to that long.
- **An unmatched route's 404 is Starlette's, not the app's envelope.** Routing renders
  `{"detail":"Not Found"}`. The app's `{"error":{…}}` handler is registered for
  `fastapi.HTTPException` only. A firewall that raised the FastAPI class would answer a body an
  observer could tell apart from a path that does not exist.
- **A wrong method on an existing path is 405 for everyone**, before any dependency runs. With a
  401 for anonymous callers, that means the *existence* of an admin path is discoverable whatever
  the firewall answers.

## Decision

1. **One role per user, `user | admin`.** `Role` is a closed `StrEnum` in `domain/identity`, held on
   `User`, stored as `identity_user.role VARCHAR(16) NOT NULL` with `ck_identity_user_role_known`
   and a **permanent `DEFAULT 'user'`**. The default keeps the previous image's registrations valid
   during the deploy window; the domain still sets the role explicitly at registration. Lower-case
   values, like every other `StrEnum` here. Admin includes user by construction: no user route asks
   for `role == user`, and the only role check is `User.is_admin`.
2. **The role is not in the access token.** It is read from `identity_user` on every admin request
   (`UserRepository.get`, one primary-key read, no new port method), so a grant or a revoke takes
   effect on the next request. The token stays at five claims. A request already past the firewall
   when a demotion commits finishes as admin; 4.2 decides whether destructive actions re-check under
   lock.
3. **`/api/admin/*` sits behind a router-level `require_admin`.** It depends on `require_user`, so
   the order is: bearer (401) → user exists (401 `not_signed_in`) → role (404). A bad bearer is
   refused before any database read. A structural walk over the live app's routes pins the
   dependency on every `/api/admin` route, with a positive control so it cannot pass vacuously. 4.1
   ships one route, `GET /api/admin/access` → 204, as the SPA's gate and the walk's real subject.
4. **A non-admin's refusal is Starlette's unmatched-route 404, byte for byte.** `require_admin`
   raises `starlette.exceptions.HTTPException(404)`, the one place in the codebase that does not use
   the error envelope, and the raise site says why. Not 403: a 403 tells a signed-in user that
   something here is for someone else. **Accepted residue:** 401-for-anonymous and routing's 405
   still reveal that an admin path exists. The 404 hides only whether *you* hold the role. The
   public repository and the SPA bundle name `/admin` anyway.
5. **Roles change only through the CLI.** `grant-role --user-id <uuid> [--dry-run]` and
   `revoke-role --user-id <uuid> [--dry-run]` run `ChangeUserRole` under `FOR UPDATE`, behind the
   same foreign-database guard as `erase-account`. There is no HTTP writer, and a test proves the
   column is written only by that use case. The last admin may be revoked; recovery is the CLI.
6. **No audit table.** The `UserRoleChanged` event and the CLI's run line are the record. They carry
   ids and role names only. A refused admin request logs one `info` line,
   `identity.admin_access_refused`, with the user id, the method and the route template.
7. **Users are told the operator can read stored data** (Constitution §8). An admin will read CV and
   document bodies from 4.3 on; the disclosure lands with the role, before any such feature.

## Alternatives

- **The role as a JWT claim.** Saves one primary-key read per admin request. Rejected: a demotion
  would wait up to 15 minutes, and a role change would become a token-invalidation problem
  (ADR-0020 revokes logins by deleting rows, not by tracking tokens).
- **403 for a non-admin.** The conventional answer. Rejected: it confirms that a signed-in user
  found something they may not use. So is the app's own 404 envelope, which is a 403 by another
  name.
- **A permission table** (per-action grants). Rejected: there is one grantable role and no action
  that needs a finer grant. A second role adds a value to the enum; a real permission model is its
  own ADR.
- **A separate admin login.** A second credential surface, a second password store and a second
  `Origin` set, for one operator who already has an account. Rejected by the owner's decision 1.
- **An HTTP endpoint for granting.** Rejected: privilege escalation would then need only a stolen
  admin session. Through the CLI it needs the box, and whoever holds the box already holds
  everything.

## Consequences

- **Easy:** a demotion takes effect on the next request; the token, the login family and the
  `Origin` set are untouched; a new admin route inherits the firewall from the router, and the walk
  catches one that is mounted elsewhere.
- **Cost:** one primary-key read on every admin request, measured on the production image (AC-38).
  Non-admin requests pay nothing.
- **Honest residue:** the existence of `/api/admin` is discoverable (401, 405). Hiding it would need
  a 404 for anonymous callers too, which breaks the client's refresh-on-401.
- **Hard:** the first admin is bootstrapped over SSH (`grant-role` with the owner's own id, read
  from `GET /api/auth/me`). Revoking the last admin is allowed and silent; the fix is the same
  command.
- **Watch:** a second role (add `--role` to the CLI and a value to the CHECK), and 4.2's destructive
  actions, which must decide whether to re-check the role inside their own transaction.
