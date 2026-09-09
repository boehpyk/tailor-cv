"""SQLAlchemy adapters implementing the domain's repository ports (ADR-0007).

One module per aggregate, mirroring `mapping/`: `repositories/<context>/<aggregate>.py`. Each class
takes an `AsyncSession` in its constructor, hides it completely (no `Session` object is ever handed
back to a caller), and implements the matching `Protocol` in `domain/<context>/ports.py` structurally
— there is no base class to inherit from, since `typing.Protocol` conformance needs none.
"""

from __future__ import annotations
