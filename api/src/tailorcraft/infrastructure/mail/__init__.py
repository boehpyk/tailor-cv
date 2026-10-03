"""Account mail (slice 2.5, ADR-0026): rendering an `AccountMail` into a message, and sending it by
SMTP submission. The only modules in `src/` that import `smtplib`, `ssl` or `email`."""

from __future__ import annotations
