"""Use cases for the `export` bounded context: turning a run's current document into something a
visitor can keep.

Seven modules, and — as in `tailoring` — the split is along the **process** that calls them rather
than along the aggregate they touch. This context goes one further than that one did, because it is
the first whose two delivery shapes are not two views of one thing: an `md` or `txt` download and a
`pdf` or `docx` download look identical to a user and share no row, no worker and no file
(ADR-0016 (a)).

- `request_export` — the API's write path for a **queued** format. Composes
  `GetTailoringRunForSession`, so the authorization rule that use case carries applies here too,
  and creates one `ExportJob`. It does **not** enqueue; the router does, after the commit
  (ADR-0014 §5).
- `render_export_job` — the worker's path. Uses `ExportJobRepository` and `TailoringRunRepository`
  **directly**, because the job it loads already encodes the authorization decision
  `request_export` made and committed.
- `render_document_inline` — the API's path for an **inline** format. Renders inside the request
  and returns bytes: no repository, no aggregate, no row, no file, no transaction of its own.
- `abandon_stale_export_jobs` — Celery beat's path, for a job whose worker was lost (X-29).
  Deliberately **not** generalized with `tailoring`'s sweep; its own docstring says why.
- `get_export_job` / `list_exports_for_run` / `download_export_file` — the API's read paths,
  mirroring `intake`, `posting` and `tailoring`.

The contrast between `request_export` and `render_export_job` is the same one
`request_tailoring_run` and `execute_tailoring_run` draw, made a second time for a second
aggregate; each of the two docstrings states it by pointing at the other, because reading only one
of them is how the reason gets lost.

This package re-exports nothing. The import-cycle hazard that
`domain/export/__init__.py` documents lives one layer down and does not reach here, but the empty
`__init__` is the convention in every other application package and there is no reason for this one
to be the exception.
"""
