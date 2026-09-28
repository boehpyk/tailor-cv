"""Read-side query adapters (ADR-0024): Core SQL projections behind a query port, never a repository.

A repository is the persistence of one aggregate, and a read model that joins several contexts'
tables is not one. The adapters here return flat read models, name every column they select, and
are the one place a cross-context join is legitimate — because nothing ever writes through them.
"""
