# ADR-0034: An admin reads and deletes a user's content through the user's own use cases, naming the owner by path; CV text crosses the wire only to the admin

- **Status:** Accepted
- **Date:** 2026-10-11
- **Relates to:** ADR-0008 (auth — **amended** the same day: amendment (k), a requester other than
  the credential's principal, behind `require_admin` only), ADR-0022 (ownership — note added),
  ADR-0023 (the workspace follows the credential — note added), ADR-0032 (the admin firewall — note
  added). Supersedes nothing.

## Context

Slice 4.3 (`admin-content`) lets an admin read a registered user's saved CVs, job postings, tailoring
runs and their exports, and delete a saved CV or a history entry, so that support no longer means
`psql` and the uploads volume over SSH. ADR-0032 decision 7 already told users an admin will read
these bodies; the operator-access sentence (Constitution §8) is on the workspace and `/account`.

Every behaviour this needs already exists as a use case behind `/api/me/`: authorize a row against
an owner, read it, render a document inline, list and serve exports, delete a saved CV, erase a
history entry. Each authorizes by one equality, `row.owner == requester` (ADR-0022), and resolves a
`UserOwner` to an existing user first (`UserNotFound` for an erased one).

Two forces pull against reuse. The `/api/me/` routes take the requester from the bearer, and
ADR-0008 (f) says a route answers to one credential and authorizes its own principal. And the
user's saved-CV schema carries `character_count` but never the extracted text — the user has the
file; an admin supporting them does not.

## Decision

1. **Behind `require_admin` only, a route may pass `UserOwner(<path user_id>)` as the requester of
   an unchanged use case.** The use case's equality authorizes the row against the target; a row
   owned by anyone else, or by a guest, is the kind's 404; an erased target is `UserNotFound`,
   translated **locally** to 404 `user_not_found` (4.2's helper — the global map's 401 would sign
   the admin out). It is still one credential (the bearer), not a transfer route (no row moves), and
   an AST scan pins where a path-built owner may appear (`routers/admin_content.py` only).
2. **No admin variant of a use case and no `AdminOwner`.** A second read or delete path per kind
   would drift from the user's; an owner that matches everything turns an equality into a role check
   inside every use case. The admin's authority is the firewall, nothing else.
3. **CV text crosses the wire to the admin, never to the user.** One admin schema,
   `AdminSavedBaseCvResponse`, is `SavedBaseCvResponse` plus `extracted_text`; the user's schemas are
   unchanged and a test pins their fields. Every admin response is `Cache-Control: no-store`. Bodies
   — CV text, posting text, both tailored documents — are shown as **plain text**, never rendered as
   Markdown or HTML, and a posting's source URL is text, not a link.
4. **Content is reached per account**, under `/api/admin/users/{user_id}/…`. A guest's rows are
   unreachable by construction (the requester is always a `UserOwner`); guests are 4.4's.
5. **Read, plus the two deletes that exist** (`DeleteSavedBaseCv`, `EraseHistoryEntry`), through the
   same committing providers as the user's routes. They answer **200 with their report** (the user's
   routes answer 204) so an unlink failure reaches the operator without SSH.
6. **Every admin content request logs one `admin.content_access` line** at `info`: actor id, target
   id, item kind, item id, action, outcome — ids and closed words only. No audit table (ADR-0032
   decision 6 unchanged), no actor in any domain event, no notification to the user.

## Alternatives

- **Admin-specific use cases** (`get_any`, a repository method without an owner). Rejected: four new
  read paths and two delete paths that must agree with the user's for ever; the drift test that
  would guard them is the reuse this decision already has.
- **An `AdminOwner`** that every equality accepts. Rejected: authorization stops being data and
  becomes a branch in every use case, and a bug in one branch reads every account.
- **A global cross-account content view** (all CVs, all runs). Rejected for this slice (OQ-1): new
  query ports per kind and a wider blast radius for a support task that always starts from one user.
- **Serving the original upload.** Rejected (OQ-4): the extracted text is what the product used; the
  original file is a second copy of the PII with nothing to add.
- **Rendering documents as HTML** (the editor, or a Markdown renderer). Rejected (OQ-5): a stranger's
  text rendered in an admin's session is the one place it could do harm, and the chunk would carry
  TipTap.
- **No read logging.** Rejected (OQ-3): reading a CV is the act a user would most want accounted for.
- **204 deletes**, as the user's. Rejected (OQ-8): the report is the operator's only signal that a
  file survived its row.

## Consequences

- **Easy:** the admin router has no business logic; a fix to a use case reaches both surfaces.
  `GET` for the admin and the user on the same data returns the same body (a test compares them).
- **Hard:** the `UserNotFound` → 401 mapping is global, so every admin content handler must
  translate it; a handler that forgets signs the admin out on an erased target.
- **Privacy:** CV text sits in the admin's tab memory until TanStack's `gcTime`, sign-out or the
  account's erasure; `no-store` keeps it out of the HTTP cache. The admin's download folder is
  outside retention — that is the explicit click's purpose.
- **Watch:** a new path-built owner outside `admin_content.py` (the AST scan fails); a user-facing
  schema gaining `extracted_text`; a body rendered through `dangerouslySetInnerHTML`.
