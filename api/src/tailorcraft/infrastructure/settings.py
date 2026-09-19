"""The one place in this codebase that reads the environment.

Constitution §8: nothing else calls `os.environ`. A configuration value that can be read from
anywhere will eventually be read from the domain layer, and then the domain depends on a deployment
detail through a channel no type checker can see.

Everything else receives what it needs — a URL, a timeout, a directory — as an argument. That is
also what makes a test able to point the same code at a different database without monkey-patching
a module global.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["production", "dev", "test"]


class MisconfiguredSettings(RuntimeError):
    """A configuration that must stop the process at startup rather than fail later, in public.

    **Deliberately not a `ValueError`**, and the reason is a footgun worth stating once here rather
    than rediscovering in a crash log. Pydantic wraps a `ValueError` raised inside a validator into a
    `ValidationError`, and a `ValidationError`'s rendered message carries `input_value` — which for a
    model validator on this class is *the settings input dict*, the object holding `DATABASE_URL`
    with its password, `REDIS_URL` with its password, `JWT_SIGNING_KEY` and the very API key the
    check is complaining about.

    Measured rather than assumed, because the detail matters: pydantic **truncates** that repr, so
    what a `ValueError` here actually prints is something like
    `input_value={'database_url': 'postgre...'app_env': 'production'}`. On the shape tested, no
    secret survived the truncation.

    **That is precisely why this is not a `ValueError`.** "No secret leaked" is not a property of the
    design here, it is an accident of how many fields this class has, what order they fall in and how
    long their values are — every one of which changes the moment someone adds a setting. A guard
    that prints a truncated dump of every secret the process holds, and is safe only because the
    truncation happened to land in a lucky place, is not a guard anyone should have to re-measure
    after each new field. Raising something pydantic does not wrap removes the question.

    Any exception that is not a `ValueError` or an `AssertionError` propagates out of a pydantic
    validator untouched (verified against the installed pydantic, not assumed), so this one carries
    exactly the sentence it was given and nothing else. It still kills uvicorn and the Celery worker
    at import of the composition root, which is the whole point.
    """


class Settings(BaseSettings):
    """Runtime configuration, validated once at startup.

    Validated *at startup* on purpose: a missing or malformed setting should stop the process
    immediately and loudly, not surface an hour later as an `AttributeError` inside a Celery task
    that nobody is watching.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env carries vars for other services (POSTGRES_*, image tags)
    )

    # -- Application ---------------------------------------------------------
    # The SAFE value is the default. Dev-ness comes from loading docker-compose.dev.yml, which pins
    # APP_ENV=dev in `environment:` — that outranks `env_file:`, so being a dev box follows the
    # override file you loaded rather than a value someone remembered to change in a gitignored
    # file. Do not "fix" this default to "dev".
    app_env: Environment = "production"
    log_level: str = "info"
    public_base_url: str = "http://localhost:8080"
    cors_origins: str = ""

    # -- Database ------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://tailorcraft:tailorcraft@postgres:5432/tailorcraft"
    # A DEDICATED test database, never the dev one. `make test` runs against this and the suite
    # truncates and rolls back inside it freely.
    test_database_url: str = (
        "postgresql+asyncpg://tailorcraft:tailorcraft@postgres:5432/tailorcraft_test"
    )

    # -- Redis: broker, result backend, cache, rate limiter ------------------
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/1"
    celery_result_backend: str = "redis://redis:6379/2"

    # -- Storage & retention (ADR-0006) --------------------------------------
    upload_dir: Path = Path("/var/lib/tailorcraft/uploads")
    max_upload_bytes: int = 10 * 1024 * 1024
    # FR-6. A privacy promise, not a tuning knob: raising it needs a reason a user would accept.
    guest_retention_hours: int = 24

    # -- CV text extraction (ADR-0009) ---------------------------------------
    # The backstop for a pathological file, not the mechanism: the page cap below bounds the work
    # *before* parsing starts, so this timeout should fire only on something genuinely stuck.
    extraction_timeout_seconds: int = 10
    # PDFs over this many pages are refused before `pypdf` ever opens them — bounded work, not a
    # timeout discovered the hard way on a 400-page file (ADR-0009 §2).
    max_cv_pages: int = 50

    # -- Upload rate limiting & caps (F-16, F-23, F-24) -----------------------
    # Fixed-window limits enforced by `RedisFixedWindowRateLimiter`
    # (`infrastructure/rate_limit.py`). Two limits, not one: per-session bounds one guest's use,
    # per-IP bounds one network's use across many guest sessions (a guest can always mint a new one).
    upload_rate_limit_per_hour: int = 10
    upload_rate_limit_per_ip_per_hour: int = 30
    # A cross-aggregate cap enforced in the use case, not on `BaseCv` itself — see technical-plan.md,
    # "Not an invariant of BaseCv, deliberately".
    max_base_cvs_per_session: int = 5
    # How many reverse-proxy hops in front of this process are ours to trust when reading
    # `X-Forwarded-For` (`infrastructure/rate_limit.py::client_ip`). `1` is nginx. Raising this
    # without actually adding a trusted proxy in front of nginx turns the rate limiter's IP bucket
    # into an attacker-chosen value read straight out of a client-supplied header.
    trusted_proxy_hops: int = 1

    # -- Job-posting intake: the guarded egress (slice 1.2, ADR-0012) ---------
    # Every bound below is a bound on what ONE outbound request to a host a stranger chose may cost
    # us. None of them is a security control that can be switched off: there is deliberately no
    # setting here that disables the SSRF address policy, no `allow_private_fetch_targets`, no "dev
    # mode" bypass. A flag that turns off a security control is a flag someone eventually sets in
    # production (ADR-0012, "The SSRF guard has no off switch"). The seam that makes the fetcher
    # testable is a constructor argument with a strict default, and a wiring test asserts the
    # production default is the strict one.
    #
    # The hard outer bound on one fetch. Per-phase timeouts are what a hostile server evades by
    # trickling one byte before each read deadline; this is the one that actually stops it.
    posting_fetch_timeout_seconds: int = 10
    posting_fetch_connect_timeout_seconds: float = 3.0
    posting_fetch_read_timeout_seconds: float = 5.0
    # Enforced on DECODED bytes while streaming, never from `Content-Length` — that header is a
    # claim by the party we are defending against, and a chunked response carries none at all.
    posting_fetch_max_bytes: int = 2 * 1024 * 1024
    # Every hop re-runs the whole guard (scheme, host, DNS, address policy). Three is generous for
    # real job boards, which redirect once or twice for canonicalisation or a country splash.
    posting_fetch_max_redirects: int = 3
    # The backstop for a pathological document; the byte cap above is the mechanism that bounds
    # ordinary extraction work.
    posting_extraction_timeout_seconds: int = 5

    # -- Job-posting rate limits & caps (P-32, P-33) --------------------------
    # Two counters with DIFFERENT failure modes on an unreachable Redis, and the asymmetry is
    # deliberate — see `RedisFixedWindowRateLimiter`. Creating a posting fails OPEN (the cost is our
    # own database, bounded). Fetching fails CLOSED (the cost is someone else's infrastructure,
    # spent from our IP address).
    posting_rate_limit_per_hour: int = 20
    posting_fetch_rate_limit_per_hour: int = 10
    posting_fetch_rate_limit_per_ip_per_hour: int = 30
    # A cross-aggregate cap enforced in `CaptureJobPosting`, not on the aggregate — the rule spans
    # every posting a session owns, which no single `JobPosting` can know.
    max_job_postings_per_session: int = 10

    # The `MaxBodySizeMiddleware` cap for NON-multipart bodies. 30,000 characters of UTF-8 is at
    # most ~120 KB, so this refuses an absurd paste before it is parsed while leaving every legal
    # one comfortable. Separate from `max_upload_bytes` (10 MB) because a JSON body and a CV upload
    # are different sizes of legitimate, and answering `file_too_large` to a JSON request would be
    # wrong in both the number and the wording.
    json_request_max_bytes: int = 256 * 1024

    # NOT settings, deliberately: `JobPostingText`'s 100 / 30,000 character bounds. The domain must
    # not read configuration, and "a posting shorter than this is not a posting" is a rule about the
    # type rather than a deployment knob. The client's pre-validation constant is a separate,
    # clearly-marked UX-only copy that says the API is the authority.

    # -- The LLM call (slice 1.3, ADR-0004) -----------------------------------
    # The one dependency in this product that is slow, non-deterministic, priced per call and
    # carrying the user's entire CV over the wire. Every bound below follows from one of those four
    # facts, and `infrastructure/llm/gemini.py` is the only module that reads any of them.
    #
    # Empty is LEGAL in dev and test — `tailor()` then raises `LlmUnavailable` immediately, logs
    # `llm.not_configured` and makes no call (G-32), which is what lets the whole suite run with no
    # key and no possibility of a surprise bill. Empty is FATAL in production: see
    # `_refuse_to_boot_without_a_key_in_production` at the bottom of this class.
    gemini_api_key: str = ""
    # Flash, not Pro, and recorded here as a choice rather than a default someone inherited: the
    # budget is 15 seconds (Constitution §7) and the task is rewriting, not reasoning. Swapping it is
    # a decision with a latency and a quality consequence, and `ModelName` is persisted on every run
    # so the swap is visible in the history rather than inferred from a deploy date.
    gemini_model: str = "gemini-2.5-flash"
    # Per ATTEMPT, enforced with `asyncio.wait_for` around the SDK call rather than with a
    # client-level option we have not measured.
    llm_request_timeout_seconds: float = 12.0
    # The hard outer bound over the whole of `tailor()`, retries and backoff included — the layered
    # shape ADR-0012 obligation 8 used for the fetcher, and for the same reason: per-phase timeouts
    # are what a slow provider evades, and the outer bound is what actually stops it.
    #
    # **Deliberately ABOVE the 15-second budget, and that is not an oversight.** 12 s + 1 s backoff +
    # 12 s is 25 s, so a run that retries MISSES the budget. That is accepted and made visible rather
    # than hidden: the alternative — a total deadline of 15 s — would cut the second attempt off
    # part-way and turn every retryable blip into `llm_timed_out`, which is a worse answer for the
    # user and a worse signal for us. The budget is defended by watching `llm_duration_ms` (the model
    # call's own share) and the retry rate, not by a deadline that lies about what happened.
    llm_total_deadline_seconds: int = 25
    # Total attempts, not retries: 2 means one retry. Only for the four retryable classes
    # (`LlmUnavailable`, `LlmRateLimited`, `LlmTimedOut`, `LlmOutputInvalid`) — never a refusal,
    # which retried is a refusal repeated at twice the price, and never `LlmInputsTooLarge`, where
    # nothing about the second attempt would differ.
    llm_max_attempts: int = 2
    # Never retry without backoff.
    llm_retry_backoff_seconds: float = 1.0
    # Bounds a runaway generation before the documents' ceilings have to.
    #
    # Worth doing the arithmetic rather than trusting the number: 4,096 tokens is roughly 16,000
    # characters for BOTH documents plus their JSON wrapper, while `TailoredCv`'s ceiling alone is
    # 20,000. So this cap, not the value object, is what a very long CV meets first — it comes back
    # truncated, fails `parse_tailoring_response` as `not_json`, and is recorded `llm_output_invalid`.
    # That is the intended ordering (a cheap bound before an expensive one), but it means raising the
    # document ceilings without raising this would change nothing at all.
    #
    # This whole cap is the ANSWER's, because thinking is disabled (owner decision, 2026-09-11 —
    # `build_generate_config` in `gemini.py`). `gemini-2.5-flash` thinks by default and thinking
    # tokens are charged against this same number, so with thinking on, the arithmetic above would
    # be wrong and a long thought could truncate every response. Re-enabling thinking (OQ-8, if
    # `make eval` shows quality suffers) therefore means raising this cap in the same change.
    llm_max_output_tokens: int = 4096
    # The G-22 pre-flight refusal, checked in the adapter BEFORE any API call. ~6,000 tokens; with a
    # 30,000-character posting (~7,500) and the template, comfortably inside the window with room for
    # the output. **Reject, never truncate** — a run against a CV the user did not know was cut is a
    # wrong answer they cannot diagnose (the 1.2 rule, carried).
    #
    # It lives here rather than in a value object because "how much fits" is a fact about THIS model
    # and moves when the model does, which is exactly what an adapter's configuration is for.
    llm_max_cv_characters: int = 25_000

    # -- Tailoring rate limits, caps & queue (slice 1.3) ----------------------
    # This endpoint costs MONEY per call and is open to guests, so its limiter is constructed with
    # `fail_open=False` — the far end of the spectrum OQ-7 opened in 1.1 and 1.2 generalized: fail
    # open when the cost is ours and bounded, fail closed when the cost is money or somebody else's
    # infrastructure. An unauthenticated endpoint that spends money with no backstop is a funded
    # denial-of-wallet, and the first evidence would be an invoice.
    tailoring_rate_limit_per_hour: int = 10
    # Per client IP, because a guest can always mint a new session — the per-session limit alone
    # bounds nothing.
    tailoring_rate_limit_per_ip_per_hour: int = 30
    # Saving an edited document (slice 1.4, `PUT …/documents/{kind}`), per session, and this one
    # fails OPEN — the rule applied a third time: a save is one `UPDATE` of one document, at most
    # 20,000 characters, bounded by the value object, and Redis being down must not stop a person
    # saving their CV. 600/hour is ten a minute sustained: above any human typing rate through the
    # client's 1.5 s debounce, below what a script hammering a PII row should get. Chosen, not
    # measured (OQ-7). No per-IP scope on purpose — a save is always bound to a session that already
    # owns the run, so a fresh session buys nothing.
    tailoring_revise_rate_limit_per_hour: int = 600
    # A cross-aggregate cap enforced in `RequestTailoringRun`, not on `TailoringRun` — the rule spans
    # every run a session owns, which no single run can know.
    max_tailoring_runs_per_session: int = 20
    # How long a run may stay `running` before it is recorded `failed` / `abandoned`. The stale-run
    # sweep (`AbandonStaleTailoringRuns`, run by beat every minute) applies this window, and so does
    # `ExecuteTailoringRun` step 3 when a redelivered message arrives late.
    #
    # **Not a tuning knob. It must stay above Celery's hard `task_time_limit`** (180 s,
    # `TASK_TIME_LIMIT_SECONDS` in `tasks/app.py`), **and `create_celery` refuses to start otherwise**,
    # in every environment. Above the limit, a live call is killed before its run is old enough to
    # sweep. At or below it, the sweep can record a call that is still running as `abandoned`. The
    # worker's later success then fails on the table's CHECK, so the result is paid for and lost, and
    # the user is invited to pay again. Shortening this to recover interrupted runs faster is exactly
    # that mistake.
    #
    # The hard limit itself records nothing: the pool child is killed mid-call, the message is acked,
    # and the run stays `running`. The sweep is what records it, at most this window plus one beat
    # interval later.
    tailoring_stale_after_seconds: int = 300
    # A named queue from the first slice so 1.5's export tasks can land on a second one without a
    # long render starving a tailoring run. One line now; expensive to retrofit.
    tailoring_queue_name: str = "tailoring"

    # -- Export rendering, limits, caps & queue (slice 1.5, ADR-0016/0017) ----
    # TXT and Markdown render inline (string manipulation); PDF and DOCX are the expensive ones and
    # go to the worker on their own named queue, for the same reason `tailoring_queue_name` got one
    # in 1.3: a long render must not starve a tailoring run sharing the worker's slots.
    export_queue_name: str = "export"
    # Per QUEUED render (PDF/DOCX, on the worker). Must sit under the task's soft limit (120) and
    # hard limit (180) — see the ordering note on `export_stale_after_seconds` below, and the
    # second `create_celery` guard that makes the hard limit non-negotiable.
    export_render_timeout_seconds: int = 60
    # Per INLINE render (TXT/Markdown, in the API request). A 20,000-character document parses in
    # milliseconds, so this is the backstop for something pathological, not the mechanism that keeps
    # an inline render fast.
    export_inline_timeout_seconds: int = 5
    # A renderer bug (an infinite table, a runaway image) must not be free to fill the uploads
    # volume that `api` and `worker` share.
    export_max_file_bytes: int = 20 * 1024 * 1024
    # How long an export job may stay `rendering` before the stale-job sweep
    # (`AbandonStaleExportJobs`, beat, every 60 s) records it `failed` / `abandoned`. Above the hard
    # limit (180) for 1.3's reason, restated for this job: at or below it, the sweep can abandon a
    # render that is still running, and the worker's later success then meets a row the sweep
    # already decided. `create_celery` refuses to start otherwise (AC-20) — see that guard.
    export_stale_after_seconds: int = 300
    # Per session. Fails OPEN (`export:create`, `fail_open=True`): a render costs worker seconds and
    # disk of ours, bounded by the per-session cap below and the purge — there is no invoice, unlike
    # `tailoring_rate_limit_per_hour`, which fails closed because that endpoint spends money.
    export_rate_limit_per_hour: int = 30
    # Per client IP, because a guest can always mint a new session — the per-session limit alone
    # bounds nothing.
    export_rate_limit_per_ip_per_hour: int = 60
    # A cross-aggregate cap enforced in the use case, not on `ExportJob` itself — the rule spans
    # every export a session owns, which no single job can know. 2 documents x 2 formats x a
    # generous number of re-exports, bounded by the purge.
    max_export_jobs_per_session: int = 40

    # NOT settings, deliberately, and this list is the answer to "why is X not configurable?":
    #
    #   * `TailoredCv`'s and `CoverLetter`'s length floors and ceilings. They live in the value
    #     objects. The domain must not read configuration, and "a 40-word CV is not a CV" is a rule
    #     about the type rather than a deployment knob — the same reasoning that keeps
    #     `JobPostingText`'s 100/30,000 out of this file.
    #   * The 15-second budget (Constitution §7). A TARGET, measured at `/verify` against
    #     `llm_duration_ms` and `make eval`, not a knob. A budget you can edit is a budget you will
    #     edit the first time it is missed.
    #   * The client's "this is taking a while" threshold. Pure UX, and a clearly-marked constant in
    #     `web/` that says the API is the authority.
    #   * Anything that would weaken an authorization rule or a rate limit. Same rule as the SSRF
    #     policy above: a flag that turns off a control is a flag someone eventually sets in
    #     production.

    # -- Observability -------------------------------------------------------
    # Empty in dev and in CI; set on the box. Phase 0 rather than deferred, because this product has
    # silent failure paths from its first slice (roadmap).
    sentry_dsn: str = Field(default="")

    # NOTE: JWT_* is declared in .env.example but deliberately absent here. A setting with no
    # consumer is a promise the code does not keep. It lands with the slice that reads it —
    # identity (2.1). GEMINI_* was in this note until slice 1.3; it has a consumer now.

    @model_validator(mode="after")
    def _refuse_to_boot_without_a_key_in_production(self) -> Settings:
        """AC-32/G-32: `APP_ENV=production` with an empty `GEMINI_API_KEY` must not start.

        **The failure mode this prevents is the expensive one.** Without this check the process
        boots, serves, accepts an upload, accepts a posting, charges the user fifteen seconds of
        waiting and a queued task — and then every single run is recorded `failed` /
        `llm_unavailable` with a `llm.not_configured` line in a log nobody is reading. The product is
        down while every health check is green, which is the exact shape of failure this codebase
        keeps writing guards against (`/health/ready` probing Celery, the deploy verifying every
        container's image). A misconfiguration that can be caught at startup must be caught at
        startup; the alternative is catching it from a support email.

        Dev and test are **explicitly** allowed to run keyless, and that permission is the point
        rather than a leniency: it is what lets the whole suite run with no key, so a key that
        appeared in CI could not silently start spending money (`.env.example` says the same thing
        one layer out). The two halves are one decision — empty is legal exactly where a call would
        never be paid for.

        `MisconfiguredSettings` rather than a `ValueError` — see that class for why a `ValueError`
        here would print every secret this object holds into the crash log. It is raised from
        `get_settings()` during import of the composition root, with the setting's name in the
        traceback. The Celery worker and beat exit on it. uvicorn under `--workers N` does **not**:
        its supervisor respawns the failing import for ever, and the container never becomes ready.
        This was measured at T36 (see CLAUDE.md). The fix is carried to `devops`, due before the
        deploy SSH secrets are set.

        A whitespace-only key counts as empty. `" "` in a hand-edited `.env` on the box is the
        plausible typo, and "we have a key" is a claim that should not be satisfiable by a space.
        """
        if self.app_env == "production" and not self.gemini_api_key.strip():
            raise MisconfiguredSettings(
                "GEMINI_API_KEY must be set when APP_ENV=production: the tailoring endpoint is the "
                "product, and without a key every run would fail as llm_unavailable after the user "
                "had already waited for it. Set it in the box's .env (mode 600, created by hand) or "
                "run with APP_ENV=dev."
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed once.

    Cached because parsing is not free and because every caller must see the same object — two
    `Settings()` instances that disagree because the environment changed underneath them is a bug
    with no symptom until it has one.
    """
    return Settings()
