---
description: Scaffold a new feature spec folder and branch
argument-hint: <feature-name>
---

Start a new feature named `$1`.

1. Create a branch: `git checkout -b feature/$1` (from an up-to-date `main`).
2. Copy the spec templates into a new folder:
   - `cp -r docs/specs/_template docs/specs/$1`
3. Confirm the folder `docs/specs/$1/` now holds `feature-spec.md`, `technical-plan.md`, and
   `task-list.md`.

Name the slice `<context>-<what-it-does>` (e.g. `intake-base-cv-upload`, `tailoring-generate-documents`)
so the branch, the spec folder and the bounded context all read the same.

Then tell the user the feature is scaffolded and suggest running `/plan $1` to fill in the spec.
Do not write any application code yet.
