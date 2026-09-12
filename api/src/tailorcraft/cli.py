"""Operational commands, run inside the container.

These are entry points, exactly like an HTTP route or a Celery task: they parse arguments, call the
code that does the work, and translate the result to an exit code. No business logic lives here.

`purge-guests` is declared and not implemented yet — deliberately. It is in the Makefile and in the
infrastructure runbook, so a missing module would fail with a `ModuleNotFoundError` that reads like
a broken install rather than "this arrives in slice 1.6".

`eval-prompts` is implemented (slice 1.3, T37) and is **not a use case**: it is a measurement an
operator runs by hand against the real Gemini API, and it costs money. Its code lives in
`infrastructure/llm/evaluation/`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


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

    # ADR-0004: prompt quality is evaluated by hand against a committed corpus, not asserted in a
    # test. The defaults are what `make eval` runs: the whole corpus, relative to api/ (the
    # container's working directory).
    evaluate = sub.add_parser(
        "eval-prompts",
        help="run the prompt eval corpus against the real Gemini API (costs money)",
    )
    evaluate.add_argument(
        "--corpus",
        type=Path,
        default=Path("eval/corpus"),
        help="corpus directory containing corpus.toml (default: eval/corpus)",
    )
    evaluate.add_argument(
        "--only",
        action="append",
        metavar="PAIR_ID",
        help="run only this pair; repeat for several (fewer paid calls while iterating)",
    )
    evaluate.add_argument(
        "--width", type=int, default=160, help="report width for the side-by-side columns"
    )
    evaluate.add_argument(
        "--validate-only",
        action="store_true",
        help="load and validate the corpus, print its sizes, and make no API call",
    )

    args = parser.parse_args(argv)

    if args.command == "purge-guests":
        return _not_yet("purge-guests", "roadmap slice 1.6, retention-guest-purge")
    if args.command == "eval-prompts":
        # Imported here, not at the top: it pulls in the Gemini SDK, which `purge-guests` has no
        # reason to load.
        from tailorcraft.infrastructure.llm.evaluation.runner import run_from_cli

        return run_from_cli(
            corpus_dir=args.corpus,
            only=args.only,
            width=args.width,
            validate_only=args.validate_only,
        )

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
