"""Operational commands, run inside the container.

These are entry points, exactly like an HTTP route or a Celery task: they parse arguments, call the
code that does the work, and translate the result to an exit code. No business logic lives here.

`purge-guests` is implemented (slice 1.6, `retention-guest-purge`, T23) and is the operator's half
of FR-6: the scheduled Celery tick and this command run the same use cases, and the runbook's three
steps — `make purge.dry`, `make purge limit=N`, `make purge` — are this command with three sets of
arguments. Its code lives in `infrastructure/retention/purge_command.py`, which is also where the
four exit codes are defined; `--orphans` selects the second use case, the volume sweep that is
**only** ever run from here and never on beat.

`revoke-logins --all [--dry-run]` is implemented (slice 2.1, `identity-register-and-login`, T33):
the break-glass that deletes every `Login` (AC-13, OQ-5). Its code lives in
`infrastructure/identity/revoke_logins_command.py`; exit 0 on success (including zero logins), 1 on
a database failure, 2 for a usage error — `--all` is required.

`check-settings` is implemented (slice 2.1, T46, OQ-2): it runs every startup refusal the API
process has — the `Settings` validators and the Celery stale-window checks — without starting
anything, and exits 1 printing the refusal's sentence (never a value) or 0 printing `settings ok`.
The production `api` command runs it before `exec uvicorn`, so a refusal exits the container.
Its code lives in `infrastructure/check_settings_command.py`.

`eval-prompts` is implemented (slice 1.3, T37) and is **not a use case**: it is a measurement an
operator runs by hand against the real Gemini API, and it costs money. Its code lives in
`infrastructure/llm/evaluation/`.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _positive_int(raw: str) -> int:
    """An argparse `type` that refuses zero and below, so a bad bound is a **usage** error.

    `--limit 0` and `--grace-hours -1` are mistakes, not instructions, and the difference matters
    here more than it usually does: a command that deletes things should refuse an argument it
    cannot honour rather than pick an interpretation. Raising `ArgumentTypeError` puts the refusal
    on argparse's own path, which exits **2** — AC-20's usage code — with the flag's name in the
    message.
    """
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, not {value}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tailorcraft")
    sub = parser.add_subparsers(dest="command", required=True)

    # FR-6 / ADR-0006. `--dry-run` and `--limit` exist because this command deletes rows AND unlinks
    # files, and neither is reversible: the runbook rehearses it in three steps rather than
    # discovering it in one (docs/infrastructure.md).
    purge = sub.add_parser("purge-guests", help="delete expired guest sessions and their files")
    purge.add_argument("--dry-run", action="store_true", help="report only; delete nothing")
    purge.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help=(
            "take at most N sessions in exactly one batch (with --orphans: consider at most N "
            "files); without it, batches loop until the backlog stops falling"
        ),
    )
    # ADR-0018: the volume sweep is operator-run only. It is a flag on this command rather than a
    # command of its own because it is the *recovery* for the crash window the purge deliberately
    # chose — a committed row delete followed by a process death before the unlink (R-6) — and an
    # operator who is here to clear guest data is the operator who needs it.
    purge.add_argument(
        "--orphans",
        action="store_true",
        help="instead of purging sessions, reclaim files on the volume that no row references",
    )
    # **No setting backs this**, decided at T19: it is a judgement about a particular volume on a
    # particular day, made by the person typing the command, and a settings field would make it a
    # value somebody set once and nobody re-read. The default is the window again, so the floor is
    # 48 h with a 24 h window. Ignored without --orphans.
    purge.add_argument(
        "--grace-hours",
        type=_positive_int,
        default=24,
        help=(
            "with --orphans: how far beyond the retention window a file must be before it is "
            "considered at all (default: 24)"
        ),
    )

    # AC-13 / OQ-5: the break-glass. Rotating `JWT_SIGNING_KEY` logs nobody out (I-44); this does.
    # `--all` is **required**, so the bare command is argparse's usage error (exit 2) rather than a
    # mass sign-out — the flag is the command's whole safety, since it has no other mode.
    revoke = sub.add_parser("revoke-logins", help="delete every login, signing every user out")
    revoke.add_argument(
        "--all",
        dest="revoke_all",
        action="store_true",
        required=True,
        help="required: revoke every login (there is no narrower mode)",
    )
    revoke.add_argument("--dry-run", action="store_true", help="report the count; delete nothing")

    # T46 / OQ-2: every startup refusal the API has, asked once in one process before uvicorn is
    # exec'd — so a refusal exits the container instead of respawning under `--workers N`.
    sub.add_parser(
        "check-settings",
        help="exit 1 if the settings would refuse to start (prints the refusal, never a value)",
    )

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
        # Imported here, not at the top, exactly as `eval-prompts` is and for the mirror-image
        # reason: that module pulls in the Gemini SDK, and this one builds a database engine and —
        # on the deleting path only — a Celery app. Neither command has any reason to load the
        # other's world.
        from tailorcraft.infrastructure.retention.purge_command import run_from_cli as run_purge

        return run_purge(
            orphans=args.orphans,
            dry_run=args.dry_run,
            limit=args.limit,
            grace_hours=args.grace_hours,
        )
    if args.command == "revoke-logins":
        # Imported here: it builds a database engine, which neither other command's path needs.
        from tailorcraft.infrastructure.identity.revoke_logins_command import (
            run_from_cli as run_revoke,
        )

        return run_revoke(dry_run=args.dry_run)
    if args.command == "check-settings":
        # Imported here: it must build nothing but `Settings`, and loading the other commands'
        # modules would import a database driver it has no use for.
        from tailorcraft.infrastructure.check_settings_command import run_from_cli as run_check

        return run_check()
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
