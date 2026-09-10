"""Use cases for the `tailoring` bounded context: requesting a run, executing it, and reading runs
back.

Four modules, and the split is along the **process** that calls them rather than along the aggregate
they touch — which is new here, because this is the first aggregate in the codebase written by one
process and read by another:

- `request_tailoring_run` — the API's write path. Composes the two read use cases, so the
  authorization rule they carry applies to this entry point too.
- `execute_tailoring_run` — the worker's path. Uses the two repositories **directly**, because the
  run it loads already encodes the authorization decision that `request_tailoring_run` made and
  committed.
- `get_tailoring_run` / `list_tailoring_runs` — the API's read path, mirroring `intake` and
  `posting`.

The contrast between the first two is deliberate and each of the two docstrings states it, pointing
at the other. Reading only one of them is how the reason gets lost.
"""
