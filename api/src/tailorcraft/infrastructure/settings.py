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
