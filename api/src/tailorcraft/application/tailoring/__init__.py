"""Use cases for the `tailoring` bounded context: requesting a run, executing it, sweeping up a run
whose worker was lost, and reading runs back.

Six modules, and the split is along the **process** that calls them rather than along the aggregate
they touch — which is new here, because this is the first aggregate in the codebase written by one
process and read by another (and, since the sweep, by a third):

- `request_tailoring_run` — the API's write path. Composes the two read use cases, so the
  authorization rule they carry applies to this entry point too.
- `execute_tailoring_run` — the worker's path. Uses the two repositories **directly**, because the
  run it loads already encodes the authorization decision that `request_tailoring_run` made and
  committed.
- `abandon_stale_tailoring_runs` — Celery beat's path. Records `failed` / `abandoned` on a run that
  claims to be `running` past the stale window, because redelivery alone does not bring a lost run
  back (G-25'). It asks `TailoringRun.is_stale`, the same rule the worker's path uses.
- `get_tailoring_run` / `list_tailoring_runs` — the API's read path, mirroring `intake` and
  `posting`.
- `revise_tailored_document` — the editor's write path (slice 1.4, ADR-0015). Composes
  `get_tailoring_run` for the same reason `request_tailoring_run` composes its two reads: a new
  entry point inherits the authorization rule instead of re-implementing it.

The contrast between the first two is deliberate and each of the two docstrings states it, pointing
at the other. Reading only one of them is how the reason gets lost.
"""
