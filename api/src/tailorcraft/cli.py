"""Operational commands, run inside the container.

These are entry points, exactly like an HTTP route or a Celery task: they parse arguments, call an
application use case, and translate the result to an exit code. No business logic lives here.

Both subcommands are declared and neither is implemented yet — deliberately. They are in the
Makefile and in the infrastructure runbook, so a missing module would fail with a
`ModuleNotFoundError` that reads like a broken install rather than "this arrives in slice 1.6".
"""

from __future__ import annotations

import argparse
import sys


def _not_yet(name: str, arrives_in: str) -> int:
    print(f"`{name}` is not implemented yet — it arrives with {arrives_in}.", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tailorcraft")
    sub = parser.add_subparsers(dest="command", required=True)

    # FR-6 / ADR-0006. `--dry-run` and `--limit` exist because this command deletes rows AND unlinks
    # files, and neither is reversible: the runbook rehearses it in three steps rather than
    # discovering it in one (docs/infrastructure.md).
    purge = sub.add_parser("purge-guests", help="delete expired guest sessions and their files")
    purge.add_argument("--dry-run", action="store_true", help="report only; delete nothing")
    purge.add_argument("--limit", type=int, default=None, help="delete at most N sessions")

    # ADR-0004: prompt quality is evaluated by hand against real postings, not asserted in a test.
    sub.add_parser("eval-prompts", help="run the prompt eval corpus against the real Gemini API")

    args = parser.parse_args(argv)

    if args.command == "purge-guests":
        return _not_yet("purge-guests", "roadmap slice 1.6, retention-guest-purge")
    if args.command == "eval-prompts":
        return _not_yet("eval-prompts", "roadmap slice 1.3, tailoring-generate-documents")

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
