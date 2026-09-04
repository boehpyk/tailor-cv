"""`TypeDecorator`s that round-trip domain value objects through single columns (ADR-0007).

A value object is one concept, so it maps to one column with a type that knows how to translate it —
not to a composite, and not to a scattering of primitive columns the application has to reassemble.

Empty in Phase 0; the first one arrives with the first value object that needs persisting.
"""
