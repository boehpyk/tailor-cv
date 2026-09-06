---
description: Run all quality gates and review a feature (Verify phase)
argument-hint: <feature-name>
---

Verify the feature `$1` against the Definition of Done (Constitution §6).

1. Run the full gate chain: `make check` — Ruff, mypy strict, import-linter, pytest, and the frontend
   checks. Fix any failures via the owning agent, then re-run.
2. Delegate to the **reviewer** agent with the list of changed files (`git diff --name-only main...`).
   It returns PASS or NEEDS CHANGES with CRITICAL/MAJOR/MINOR/STYLE findings.
3. Check every acceptance criterion in `docs/specs/$1/feature-spec.md` off against the implementation,
   including every row of the failure contract — those are the ones a green suite most often misses.
4. **Audit the red-first discipline** (sdlc.md §2). For every task marked RED in
   `docs/specs/$1/task-list.md`: it carries a `Recorded red:` line, that recorded failure is an
   **assertion** and not an `ImportError`, and the commit that follows it turned the test green
   **without editing the test**. Check the last with
   `git log -p --follow -- <test file>` — a test modified in its own GREEN commit is a CRITICAL
   finding, because it is the exact failure red-first exists to prevent. A missing recorded red means
   the cycle was skipped: the test is unproven, not wrong, so re-derive it from the acceptance
   criterion rather than trusting it.
5. If this slice touched the tailoring path, confirm the **15-second** end-to-end budget was actually
   measured, not assumed (Constitution §7). A budget checked once is a budget you no longer have.

**Escalation rule:** if the reviewer returns NEEDS CHANGES (any CRITICAL or MAJOR), send the findings
back to the owning agent, fix, and re-verify — up to **3 iterations**. After 3 without PASS, **stop
and bring it to the user**: repeated failure means the spec or design is wrong, not the code.

On PASS with all criteria met: confirm the feature is done, remind the user to update docs
(CLAUDE.md / ADR / FORboehpyk.md) where behaviour changed, and that the spec in `docs/specs/$1/` may
now be archived — its behaviour lives in tests and code.
