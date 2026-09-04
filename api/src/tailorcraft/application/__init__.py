"""The application layer: one use case per user intent.

A use case loads an aggregate through a port, calls methods on it, persists it through the same
port, and publishes whatever the aggregate recorded. It orchestrates; it does not decide. A rule
expressed here rather than in an aggregate is the anemic-model smell, and it is the most common way
a domain layer quietly becomes a bag of data classes.

Dependencies arrive as **ports, through the constructor**. This layer imports `tailorcraft.domain`
and the standard library — never `tailorcraft.infrastructure`, never a session, never a vendor SDK
(ADR-0002). That constraint is what lets a use case be tested with no database and re-pointed at a
different vendor with no edit.

Empty in Phase 0 by design: the roadmap's Phase 0 is scaffolding, and the first use case arrives
with slice 1.1, `intake-base-cv-upload`.
"""
