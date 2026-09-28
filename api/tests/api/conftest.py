"""Map the aggregates before any module in this directory is imported.

The persistence repositories read mapped attributes (`JobPosting._id`, `TailoringRun._id`, …) at
import time, and the root conftest's `_mappings` fixture runs only after collection. A test module
that imports a repository ahead of every mapping module would otherwise fail to collect — an
`AttributeError` that reads like a missing column. Running `configure_mappings()` here, at conftest
import, settles it once for `tests/api/` instead of in each module's import order.
"""

from __future__ import annotations

from tailorcraft.infrastructure.persistence.registry import configure_mappings

configure_mappings()
