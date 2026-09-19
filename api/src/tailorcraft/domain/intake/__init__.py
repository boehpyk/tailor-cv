"""The `intake` bounded context: accepting a base CV, extracting its text, and owning that outcome.

**This package `__init__` re-exports nothing, and that is load-bearing rather than tidy.**
`domain/shared/files.py` imports `BaseCvId` and `CvContentType` from
`tailorcraft.domain.intake.value_objects` so that `FileRef.for_base_cv` can derive a storage key
from a CV's own id — while `base_cv.py`, in this same package, imports `FileRef` back out of
`files.py`. That is only *not* a cycle because importing
`tailorcraft.domain.intake.value_objects` runs this module first and this module runs nothing else:
it never touches `base_cv.py`. Add a single `from .base_cv import BaseCv` here and the two modules
start importing each other at import time, which surfaces as an `ImportError` at application
startup rather than anywhere near the edit that caused it.

`domain/export/__init__.py` is empty of re-exports for the identical reason — `FileRef` imports
`ExportJobId`, `ExportFormat` and `ExportFormatNotQueued` out of `export` for `for_export` — and
`files.py`'s own module docstring is where that guarantee is written down for both contexts. Keep
this package `__init__` free of re-exports, or give `for_base_cv` its own module instead.
"""
