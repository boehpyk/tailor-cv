"""`tailorcraft.cli check-settings` — "would this configuration start?", asked before anything starts.

**Why it exists** (slice 2.1, T46, OQ-2). A startup refusal under `uvicorn --workers N` does not exit
the container: each worker process fails at import, the supervisor respawns it for ever, and the
container never becomes ready and never exits — so `restart: unless-stopped` never cycles it and
nothing reads as a restart loop (measured in 1.3's T36). The fix is not in uvicorn; it is to ask the
question once, in a single process, **before** uvicorn is exec'd:

    sh -c "python -m tailorcraft.cli check-settings && exec uvicorn … --workers 2"

A refusal then exits the container non-zero, where `docker compose ps` and the deploy's readiness
gate can see it.

**What it runs, and it is every startup refusal the API process has** — the same code, not a copy:

| refusal | where it lives | how it is reached here |
|---|---|---|
| `GEMINI_API_KEY` empty under production | `Settings` model validator | constructing `Settings` |
| `JWT_SIGNING_KEY` weak under production (I-46) | `Settings` model validator | constructing `Settings` |
| `TEST_REDIS_URL` sharing a live Redis database | `Settings` model validator | constructing `Settings` |
| `TAILORING_STALE_AFTER_SECONDS` ≤ the hard limit | `tasks/limits.py` (called by `create_celery`) | `refuse_stale_windows_within_time_limit` |
| `EXPORT_STALE_AFTER_SECONDS` ≤ the hard limit | `tasks/limits.py` (called by `create_celery`) | `refuse_stale_windows_within_time_limit` |

Nothing is built: no engine, no Redis client, no Celery app, no logging configuration, no Sentry.

**What it prints never contains a value.** Three outcomes, three shapes:

- `MisconfiguredSettings` — its sentence, verbatim. That class exists so that the sentence is all
  there is: every one of its messages names the variable and never a secret (the stale-window ones
  name the offending *integer* and the limit, which is what makes them actionable and is not a
  secret). It is raised outside any `except`, so there is no chained context to render either.
- `pydantic.ValidationError` — **field names and error types only**. Pydantic's own `str()` carries
  `input_value`, and for a malformed `DATABASE_URL` that input *is* the password. `errors()` is read
  with `include_input=False`, and its `msg` is not printed either: a custom validator's message may
  quote what it refused.
- anything else — the exception's type only, for the reason every floor in this codebase logs the
  type and never the message.

Exit codes: **0** the settings would start (prints `settings ok`), **1** they would not.
"""

from __future__ import annotations

import sys
from typing import Final

from pydantic import ValidationError

from tailorcraft.infrastructure.settings import MisconfiguredSettings, get_settings
from tailorcraft.infrastructure.tasks.limits import refuse_stale_windows_within_time_limit

EXIT_OK: Final = 0
EXIT_REFUSED: Final = 1

_PREFIX: Final = "check-settings"


def run_from_cli() -> int:
    """`tailorcraft.cli check-settings`. Returns an exit code; never raises past `Exception`."""
    # A fresh parse, not a cached one: in a real `python -m` invocation the cache is empty anyway,
    # and in a test driving this function it must reflect the environment the test set.
    get_settings.cache_clear()
    try:
        settings = get_settings()
        refuse_stale_windows_within_time_limit(settings)
    except MisconfiguredSettings as exc:
        print(f"{_PREFIX}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except ValidationError as exc:
        print(f"{_PREFIX}: {_describe_validation_error(exc)}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as exc:
        print(f"{_PREFIX}: settings could not be read ({type(exc).__name__}).", file=sys.stderr)
        return EXIT_REFUSED
    print("settings ok")
    return EXIT_OK


def _describe_validation_error(exc: ValidationError) -> str:
    """`N invalid setting(s): DATABASE_URL (url_parsing), MAX_UPLOAD_BYTES (int_parsing)`.

    The environment variable is the field name upper-cased — `Settings` has no prefix and no aliases
    — so that is what an operator editing `.env` is shown. A model-level error has an empty `loc`
    and is reported as `(settings)`.
    """
    parts = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        location = ".".join(str(part) for part in error["loc"]).upper() or "(settings)"
        parts.append(f"{location} ({error['type']})")
    noun = "setting" if len(parts) == 1 else "settings"
    return f"{len(parts)} invalid {noun}: " + ", ".join(parts)
