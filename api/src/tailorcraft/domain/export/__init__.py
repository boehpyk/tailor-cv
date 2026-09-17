"""The `export` bounded context: turning a run's current document into a file someone can keep.

**This package `__init__` re-exports nothing, and that is load-bearing rather than tidy.**
`domain/shared/files.py` imports `ExportJobId` and `ExportFormat` from
`tailorcraft.domain.export.value_objects` so that `FileRef.for_export` can exist beside
`for_base_cv` — while `export_job.py`, in this same package, imports `FileRef` back out of
`files.py`. That is only *not* a cycle because importing
`tailorcraft.domain.export.value_objects` runs this module first and this module runs nothing
else: it never touches `export_job.py`. Add a single `from .export_job import ExportJob` here and
the two modules start importing each other at import time, which surfaces as an `ImportError` at
application startup rather than anywhere near the edit that caused it.

`domain/intake/__init__.py` is empty of re-exports for the identical reason — `FileRef` imports
`BaseCvId` and `CvContentType` out of `intake` — and `files.py`'s own module docstring is where
that guarantee is written down for both contexts. Keep this package `__init__` free of
re-exports, or give `for_export` its own module instead.
"""
