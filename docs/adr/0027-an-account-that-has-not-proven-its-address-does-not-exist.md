# ADR-0027: An account that has not proven its address does not exist: a pending registration, confirmed by a one-time link

- **Status:** Accepted
- **Date:** 2026-10-02
- **Relates to:** ADR-0008 (b) (registration enumerated *"until an email channel exists"* —
  **amended (h)** alongside this ADR), ADR-0010 (a guest session is not a weak login: the precedent
  for *state that would be a flag is a different thing*), ADR-0010 §3 and ADR-0020 §7 (entropy decides
  the hash), ADR-0020 §5 (revocation is deletion — here, *use* is deletion), ADR-0021 §3 (the decoy
  verify this design does not need) and §5 (the per-address limiter key), ADR-0026 (the channel, and
  the token minted in the worker), ADR-0028 (the reset, which is how the residue below is recovered),
  ADR-0018 (the sweep that removes what expires — **amended**). Constitution §4's `identity` row is
  amended in the same commit. Supersedes nothing.

## Context

From slice 2.1 to 2.4, `POST /api/auth/register` created a `User`, signed it in and answered 409
`email_already_registered` for an address that already had one. ADR-0008 (b) stated the cost: anyone
can learn whether an address has an account here. With a mail channel (ADR-0026) the request can stop
revealing that, but only if *something* proves the address belongs to the person registering it.

The question the roadmap asked was *"what may an unverified account do?"* Four forces shape the
answer:

1. **An unverified `User` needs a capability matrix.** Sign in? Tailor as a user? Save CVs? Claim
   guest work (ADR-0025)? Every route would have to ask, and the one that forgets is the leak.
2. **Squatting.** If an unconfirmed sign-up holds the address, an attacker can register a victim's
   address first and keep them out until something removes the row.
3. **The pre-hijack.** An attacker registers *your* address with *their* password. If anything about
   the flow lets you end up working inside that account, they can read what you put in it.
4. **Mail scanners follow links.** Corporate gateways and some webmail fetch every link in a message,
   and some execute JavaScript.

## Decision

**1. A pending registration is its own aggregate, not a flag on `User`.** `PendingRegistration`
(`identity_pending_registration`) holds the normalized address, the argon2 hash of the chosen
password, `requested_at`/`expires_at`, and — once issued — a token hash and `issued_at`. Its
invariant is *issued ⇔ a token hash is present*, and `requested_at ≤ issued_at < expires_at`. **It is
not an account**: it cannot sign in, own a row or be claimed into. So the matrix in force 1 has one
answer — *nothing* — because there is nobody to ask about. `identity_user` is untouched, and
*"every `User` created since 2.5 proved its address"* is true by construction, not a predicate over a
column. The guest workspace stays fully usable while the mail is in flight.

**2. Registration's request path has no branch on the address.** Origin check → per-IP limiter →
parse the address → per-address limiter → password policy → argon2 hash → upsert the pending row
(committed) → enqueue its id → **202, empty body, no cookie**. The route never asks whether an account
exists, so there is nothing to equalise: login needs a decoy (ADR-0021 §3) because it *must* look the
account up, and registration need not, so it doesn't. The **worker**, which nobody can time, reads the
pending row and asks `UserRepository.find_by_email`: no account → issue a token and send *Confirm your
email address*; an account → delete the pending row and send the **account-exists notice** (no token;
links to log in and to reset). The requester never learns which mail went out. The proof is
structural — the request's use case makes **zero** `UserRepository` calls, pinned by a recording
double and a named mutation — plus byte-identical responses, plus timing on the production image.

**3. The 409 moves to confirmation.** If an account appears for the address between the request and
the click (a second sign-up confirmed first, or a pre-2.5 account), confirming answers 409
`email_already_registered` and deletes the pending row. The only person who can see that answer holds
a token that was mailed to the address — which is to say the address's owner.

**4. One pending registration per address; the newest wins; no resend endpoint.** The address is
unique, and the write is one `INSERT … ON CONFLICT (email) DO UPDATE` that replaces the id, the
password hash, the times, and clears the token. No `SELECT` first, so no branch and no race between
two concurrent sign-ups. A second registration supersedes the first: its task finds its id gone and
sends nothing, and its link, if already mailed, is dead. **The address is never held**: an attacker's
row is replaced by the owner's next attempt and expires on its own. *Send it again* re-submits the
registration the page still holds — the identical path by definition — rather than calling a
resend-by-address endpoint, which would have to branch on whether a row exists.

**5. The one-time token.** 256 bits from `secrets.token_urlsafe(32)` (43 URL-safe characters), stored
only as its SHA-256 (`TokenHash`, ADR-0010 §3: entropy decides the hash, a KDF would be waste),
minted **in the worker** and committed before the mail is sent (ADR-0026 decision 3). The link is
`PUBLIC_BASE_URL/confirm-email#token=…`: the token rides in the URL **fragment**, which browsers never
send to a server, so it is in no access log and no `Referer`; the landing page reads it into memory
and strips it from the address bar and history before rendering, and the document is served with
`Referrer-Policy: no-referrer`. The route hashes the presented token, so the application layer only
ever sees a `TokenHash`. **Used is deleted.** It lives 24 hours by default
(`EMAIL_CONFIRMATION_TTL_HOURS`, bounded 1–72), the same number as the guest window so the copy says
one number.

**6. Confirmation is an explicit click, and it does not sign in.** The landing page shows a button;
it never confirms on load, because a scanner that fetches the page and runs its script would
otherwise confirm for nobody. And confirming **creates the `User` and stops**: the person who chose the
password must type it. That is what defeats the pre-hijack (force 3): if you click a confirmation that
a stranger's registration sent you, an account exists in your name that you cannot log into, so you
reset its password (ADR-0028), which proves the address and deletes every login. You never work
inside an account whose password a stranger knows. The cost is one password entry after the click;
the check-your-email screen keeps `next` alive in the original tab, so 2.4's CTA → claim path still
ends at the claim offer.

**7. Confirmation is one transaction.** Lock the pending row by token hash (`FOR UPDATE`) → refuse it
if unknown or expired (expired is deleted and committed, then refused) → `User.register_with_password`
→ insert → delete the pending row → commit. Two concurrent clicks of one link produce one `User`, one
204 and one 400 `link_invalid`. `UserRegistered` is recorded by the `User` at this moment; the pending
registration records no event (its only facts are an address and a hash, which no event may carry).

**8. Accounts created before 2.5 are grandfathered.** They never proved their address and keep
working; there is no `email_verified_at` column, because nothing would read it. A reset proves the
address the day one is used.

## Alternatives

- **`User.email_verified_at`, with unverified users allowed in.** Rejected on every row that matters:
  a capability matrix every route must consult; an attacker's unverified row squatting the address
  until a purge deletes it — the first automated deletion of *accounts*; a backfill question for
  every existing row. The flag would describe a state in which the aggregate whose invariant is
  *"exactly one current credential, proven"* does not hold it.
- **Registration with two branches tuned to cost the same** (look the address up, then do equal work
  either way). Rejected: equalising is a measurement that drifts with every change; a path with no
  branch has nothing to drift.
- **Keep every pending registration for an address.** Rejected: with several live rows, a victim's
  resend could re-deliver an attacker's row, and *"which one does confirm create?"* becomes a rule.
- **A resend endpoint keyed on the address.** Rejected (decision 4): it must branch on existence, and
  its cheap path would be timeable.
- **A stateless signed link** (an HMAC or a JWT carrying the address and a hash of the password).
  Rejected: *used is deleted* and *newest wins* both need state, and a password hash would ride in a
  URL.
- **The token in a query string.** Rejected: it lands in proxy access logs and in `Referer`.
- **Confirm on page load.** Rejected: link scanners (decision 6).
- **Confirmation signs the user in.** The smoother flow, and the pre-hijack's payload. Rejected.
- **Force pre-2.5 accounts to confirm.** Rejected: production's only such account is a canary, and a
  reset already proves the address whenever it matters.

## Consequences

- **Registration takes two steps and a mailbox.** A mail outage delays account creation; it never
  breaks the request (ADR-0026), and the guest workspace works throughout.
- **Registration no longer enumerates** (ADR-0008 (h)). The per-IP limit stays and a per-address limit
  (3/h) joins it; both fail closed (ADR-0021's amendment).
- **Registration can be used to make us mail a stranger.** Bounded by the two limits, the `Origin`
  check and the content — a link or a notice, never anything the requester wrote.
- **A stranger's address can sit in a pending row** for up to the TTL plus one sweep interval
  (ADR-0018's amendment), with a password hash nobody can use. Account erasure also deletes pending
  rows by the account's address.
- **The pre-hijack residue is stated, not removed**: an account in your name can exist briefly with a
  stranger's password and no data, and the remedy is a reset.
- **`RegisterUser` is removed.** Tests that seeded users through `POST /api/auth/register` re-seed
  first, in their own commit.
- **The migration's downgrade refuses nothing.** The rows are one-time tokens living a day; losing one
  costs its owner one re-registration, and a rollback must never be blocked by a stranger's
  half-finished sign-up. After a downgrade to 2.4's code, registration enumerates again — ADR-0008
  (b)'s state, consistent with that code.
