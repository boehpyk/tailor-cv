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

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["production", "dev", "test"]


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

    # -- Observability -------------------------------------------------------
    # Empty in dev and in CI; set on the box. Phase 0 rather than deferred, because this product has
    # silent failure paths from its first slice (roadmap).
    sentry_dsn: str = Field(default="")

    # NOTE: GEMINI_* and JWT_* are declared in .env.example but deliberately absent here. A setting
    # with no consumer is a promise the code does not keep. They land with the slices that read
    # them — tailoring (1.3) and identity (2.1).

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
