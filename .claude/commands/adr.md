---
description: Create the next-numbered Architecture Decision Record
argument-hint: <short title>
---

Create a new ADR titled "$ARGUMENTS".

1. Find the highest-numbered file in `docs/adr/` and use the next number (zero-padded, e.g. `0009`).
2. Create `docs/adr/NNNN-<kebab-title>.md` following the format defined in
   `docs/adr/0000-record-architecture-decisions.md`:
   - Status (Proposed → Accepted), Date (today).
   - **Context** (forces: technical, product, learning), **Decision** (stated plainly),
     **Alternatives** (rejected, with why), **Consequences** (what it makes easy/hard, what to watch).
3. If this decision supersedes an earlier ADR, note "Supersedes ADR-XXXX" and update the older ADR's
   status to "Superseded by ADR-NNNN". If it changes the locked stack, update the tables in
   `docs/constitution.md`.

Keep it short and honest. Write the alternatives you actually considered, including the tempting one
you rejected — an ADR whose alternatives section is empty is a decision nobody can reopen safely.

A decision is recorded once and superseded later — never silently edited to change its meaning.
