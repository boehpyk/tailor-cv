"""The slice-2.5 Celery surface (T23, test-after): the two account-mail delivery tasks, the identity
token sweep task, and the beat entry that publishes the sweep (plan §0.4, §0.9, §0.10; AC-23, AC-41,
AC-43's task half).

Entry points are **thin** (ADR-0005) and each test says which promise it holds them to:

- `identity_mail.py` — one UUID string in, `None` out, `ignore_result`, **no retry of any kind**
  (the adapter owns the one retry mechanism; a Celery retry would multiply attempts and, on Redis, a
  `countdown` message is restored every `visibility_timeout` until it runs). Read from the decorator
  call's own keyword arguments and from the module's source, never from runtime defaults.
- The outcome line: `identity.mail_delivered` at INFO or WARNING, and **our own** credentials being
  refused is `identity.mail_provider_refused` at ERROR (V-27), the one that reaches Sentry.
- The sweep task: one line per run **including the empty ones**; a failure logs the exception's
  *type*, never its message, and re-raises (V-57) so Celery records it.

The use-case coroutines are replaced at the task module's own seam (`_deliver_*`,
`_sweep_identity_tokens`), because these tests are about the entry point's translation; the use cases
and their SQL are proven elsewhere against real Postgres.

Logs through `caplog`, with `configure_logging` first, and each log test asserts a record was captured
before asserting what it carries.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from tailorcraft.application.identity.delivery_outcome import DeliveryOutcome, DeliveryStatus
from tailorcraft.domain.identity.value_objects import (
    MailFailureReason,
    PasswordResetId,
    PendingRegistrationId,
)
from tailorcraft.domain.retention.value_objects import IdentityTokenSweepReport
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import app as tasks_app_module
from tailorcraft.infrastructure.tasks import identity_mail as mail_tasks
from tailorcraft.infrastructure.tasks import retention as retention_tasks

_REGISTRATION_TASK = "tailorcraft.identity.deliver_registration_mail"
_RESET_TASK = "tailorcraft.identity.deliver_password_reset_mail"
_SWEEP_TASK = "tailorcraft.retention.sweep_expired_identity_tokens"
_BEAT_ENTRY = "sweep-expired-identity-tokens"


@pytest.fixture(autouse=True)
def _structlog_through_stdlib() -> None:
    configure_logging(Settings(app_env="test"))


def _events(caplog: pytest.LogCaptureFixture) -> list[tuple[int, dict[str, Any]]]:
    """`(levelno, fields)` for every captured record whose message is a JSON event."""
    found: list[tuple[int, dict[str, Any]]] = []
    for record in caplog.records:
        try:
            fields = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(fields, dict):
            found.append((record.levelno, fields))
    return found


def _only(caplog: pytest.LogCaptureFixture, event: str) -> tuple[int, dict[str, Any]]:
    matching = [(lvl, f) for lvl, f in _events(caplog) if f.get("event") == event]
    assert matching, (
        f"no {event!r} record captured; saw {[f.get('event') for _, f in _events(caplog)]}"
    )
    assert len(matching) == 1, matching
    return matching[0]


# ------------------------------------------------------------------------------- registration


def test_the_task_names_are_the_ones_the_producer_publishes() -> None:
    assert mail_tasks.deliver_registration_mail.name == _REGISTRATION_TASK
    assert mail_tasks.deliver_password_reset_mail.name == _RESET_TASK
    assert retention_tasks.sweep_expired_identity_tokens.name == _SWEEP_TASK
    for name in (_REGISTRATION_TASK, _RESET_TASK, _SWEEP_TASK):
        assert name in tasks_app_module.app.tasks, f"the worker's registry lacks {name}"


def test_the_mail_tasks_ignore_their_result() -> None:
    assert mail_tasks.deliver_registration_mail.ignore_result is True
    assert mail_tasks.deliver_password_reset_mail.ignore_result is True
    assert retention_tasks.sweep_expired_identity_tokens.ignore_result is True


def _decorator_keywords(path: str, function: str) -> set[str]:
    tree = ast.parse(Path(path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            keywords = {
                kw.arg
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Call)
                for kw in decorator.keywords
                if kw.arg is not None
            }
            assert keywords, f"{function} has no @app.task(...) keywords at all"
            return keywords
    raise AssertionError(f"no function {function} in {path}")


@pytest.mark.parametrize("function", ["deliver_registration_mail", "deliver_password_reset_mail"])
def test_the_mail_tasks_declare_no_retry_policy(function: str) -> None:
    keywords = _decorator_keywords(str(Path(mail_tasks.__file__)), function)

    assert not keywords & {
        "autoretry_for",
        "retry_backoff",
        "retry_backoff_max",
        "retry_jitter",
        "max_retries",
        "default_retry_delay",
        "retry_kwargs",
    }


def test_the_sweep_task_declares_no_retry_policy() -> None:
    keywords = _decorator_keywords(
        str(Path(retention_tasks.__file__)), "sweep_expired_identity_tokens"
    )

    assert not keywords & {"autoretry_for", "retry_backoff", "max_retries", "default_retry_delay"}


def test_the_mail_tasks_module_never_retries_or_delays() -> None:
    """No `self.retry(...)`, no `countdown`/`eta` anywhere in the module: on Redis a delayed message
    is restored every `visibility_timeout` until it runs (`tasks/app.py`'s warning)."""
    tree = ast.parse(Path(mail_tasks.__file__).read_text())
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    keywords = {kw.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for kw in n.keywords}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}

    assert "retry" not in attributes
    assert not (keywords | names) & {"countdown", "eta", "apply_async"}


# ----------------------------------------------------------------------------- the mail tasks


def test_the_registration_task_runs_the_use_case_for_its_id_and_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[PendingRegistrationId] = []

    async def _deliver(pending_id: PendingRegistrationId) -> DeliveryOutcome:
        seen.append(pending_id)
        return DeliveryOutcome(DeliveryStatus.SENT)

    monkeypatch.setattr(mail_tasks, "_deliver_registration", _deliver)
    row_id = uuid4()

    result = mail_tasks.deliver_registration_mail.run(str(row_id))

    assert result is None
    assert seen == [PendingRegistrationId(row_id)]


def test_the_reset_task_runs_the_use_case_for_its_id_and_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[PasswordResetId] = []

    async def _deliver(reset_id: PasswordResetId) -> DeliveryOutcome:
        seen.append(reset_id)
        return DeliveryOutcome(DeliveryStatus.NO_ACCOUNT)

    monkeypatch.setattr(mail_tasks, "_deliver_password_reset", _deliver)
    row_id = uuid4()

    assert mail_tasks.deliver_password_reset_mail.run(str(row_id)) is None
    assert seen == [PasswordResetId(row_id)]


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "123"])
def test_a_malformed_id_escapes_as_an_error_for_the_error_reporter(bad: str) -> None:
    """The only publisher is `CeleryAccountMailQueue`; anything else is a bug, not an outcome."""
    with pytest.raises(ValueError, match=r"."):
        mail_tasks.deliver_registration_mail.run(bad)
    with pytest.raises(ValueError, match=r"."):
        mail_tasks.deliver_password_reset_mail.run(bad)


def test_a_failing_use_case_escapes_the_task_so_celery_records_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(pending_id: PendingRegistrationId) -> DeliveryOutcome:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(mail_tasks, "_deliver_registration", _boom)

    with pytest.raises(RuntimeError, match="commit failed"):
        mail_tasks.deliver_registration_mail.run(str(uuid4()))


def _run_with_outcome(
    monkeypatch: pytest.MonkeyPatch, outcome: DeliveryOutcome, row_id: UUID
) -> None:
    async def _deliver(pending_id: PendingRegistrationId) -> DeliveryOutcome:
        return outcome

    monkeypatch.setattr(mail_tasks, "_deliver_registration", _deliver)
    mail_tasks.deliver_registration_mail.run(str(row_id))


@pytest.mark.parametrize(
    "status",
    [
        DeliveryStatus.SENT,
        DeliveryStatus.ACCOUNT_EXISTS_NOTICE_SENT,
        DeliveryStatus.SKIPPED,
        DeliveryStatus.MISSING,
        DeliveryStatus.EXPIRED,
    ],
)
def test_a_delivery_that_went_as_designed_logs_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, status: DeliveryStatus
) -> None:
    row_id = uuid4()

    with caplog.at_level(logging.DEBUG):
        _run_with_outcome(monkeypatch, DeliveryOutcome(status), row_id)

    level, fields = _only(caplog, "identity.mail_delivered")
    assert level == logging.INFO
    assert fields["kind"] == "registration"
    assert fields["id"] == str(row_id)
    assert fields["outcome"] == status.value
    assert fields["reason"] is None
    assert fields["smtp_code"] is None
    assert isinstance(fields["duration_ms"], int)
    assert fields["duration_ms"] >= 0


def test_a_rejected_recipient_logs_info_with_its_reply_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        _run_with_outcome(
            monkeypatch,
            DeliveryOutcome(DeliveryStatus.RECIPIENT_REJECTED, smtp_code=550),
            uuid4(),
        )

    level, fields = _only(caplog, "identity.mail_delivered")
    assert level == logging.INFO
    assert fields["outcome"] == "recipient_rejected"
    assert fields["smtp_code"] == 550


@pytest.mark.parametrize("reason", [MailFailureReason.UNAVAILABLE, MailFailureReason.THROTTLED])
def test_a_send_that_may_succeed_later_logs_a_warning_with_reason_and_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reason: MailFailureReason
) -> None:
    with caplog.at_level(logging.DEBUG):
        _run_with_outcome(
            monkeypatch,
            DeliveryOutcome(DeliveryStatus.FAILED, reason=reason, smtp_code=421),
            uuid4(),
        )

    level, fields = _only(caplog, "identity.mail_delivered")
    assert level == logging.WARNING
    assert fields["outcome"] == "failed"
    assert fields["reason"] == reason.value
    assert fields["smtp_code"] == 421


def test_our_own_credentials_being_refused_is_an_error_under_its_own_event_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """V-27: every later mail fails the same way until an operator fixes the configuration, so this
    one is at ERROR where Sentry sees it, and is not also logged as an ordinary delivery."""
    row_id = uuid4()
    with caplog.at_level(logging.DEBUG):
        _run_with_outcome(
            monkeypatch,
            DeliveryOutcome(
                DeliveryStatus.FAILED, reason=MailFailureReason.PROVIDER_REFUSED, smtp_code=535
            ),
            row_id,
        )

    level, fields = _only(caplog, "identity.mail_provider_refused")
    assert level == logging.ERROR
    assert fields["kind"] == "registration"
    assert fields["id"] == str(row_id)
    assert fields["reason"] == "provider_refused"
    assert fields["smtp_code"] == 535
    assert not [f for _, f in _events(caplog) if f.get("event") == "identity.mail_delivered"]


def test_the_reset_task_logs_its_own_kind(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _deliver(reset_id: PasswordResetId) -> DeliveryOutcome:
        return DeliveryOutcome(DeliveryStatus.SENT)

    monkeypatch.setattr(mail_tasks, "_deliver_password_reset", _deliver)
    row_id = uuid4()

    with caplog.at_level(logging.DEBUG):
        mail_tasks.deliver_password_reset_mail.run(str(row_id))

    _, fields = _only(caplog, "identity.mail_delivered")
    assert fields["kind"] == "password_reset"
    assert fields["id"] == str(row_id)


# ------------------------------------------------------------------------------ the sweep task


def test_the_sweep_task_logs_one_line_with_three_counts_and_a_duration(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _sweep() -> IdentityTokenSweepReport:
        return IdentityTokenSweepReport(pending_registrations=3, password_resets=2, logins=1)

    monkeypatch.setattr(retention_tasks, "_sweep_identity_tokens", _sweep)

    with caplog.at_level(logging.DEBUG):
        result = retention_tasks.sweep_expired_identity_tokens.run()

    assert result is None
    level, fields = _only(caplog, "retention.identity_token_sweep")
    assert level == logging.INFO
    assert (fields["pending_registrations"], fields["password_resets"], fields["logins"]) == (
        3,
        2,
        1,
    )
    assert isinstance(fields["duration_ms"], int)


def test_an_empty_sweep_still_logs_its_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A job that does nothing and logs nothing is indistinguishable from one that never ran."""

    async def _sweep() -> IdentityTokenSweepReport:
        return IdentityTokenSweepReport(pending_registrations=0, password_resets=0, logins=0)

    monkeypatch.setattr(retention_tasks, "_sweep_identity_tokens", _sweep)

    with caplog.at_level(logging.DEBUG):
        retention_tasks.sweep_expired_identity_tokens.run()

    _, fields = _only(caplog, "retention.identity_token_sweep")
    assert (fields["pending_registrations"], fields["password_resets"], fields["logins"]) == (
        0,
        0,
        0,
    )


def test_a_failing_sweep_logs_the_error_type_only_and_reraises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "MARKER-row-quoted-by-the-driver-ab12"

    async def _sweep() -> IdentityTokenSweepReport:
        raise RuntimeError(marker)

    monkeypatch.setattr(retention_tasks, "_sweep_identity_tokens", _sweep)

    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match=marker):
        retention_tasks.sweep_expired_identity_tokens.run()

    level, fields = _only(caplog, "retention.identity_token_sweep_failed")
    assert level == logging.WARNING
    assert fields["error_type"] == "RuntimeError"
    assert isinstance(fields["duration_ms"], int)
    assert marker not in caplog.text
    assert not [f for _, f in _events(caplog) if f.get("event") == "retention.identity_token_sweep"]


# ------------------------------------------------------------------------------------- beat


def _built(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Any:
    settings = Settings(app_env="test", **overrides)
    monkeypatch.setattr(tasks_app_module, "get_settings", lambda: settings)
    return tasks_app_module.create_celery()


@pytest.mark.parametrize("purge_enabled", [False, True])
def test_the_sweep_is_scheduled_whatever_the_purge_flag_says(
    monkeypatch: pytest.MonkeyPatch, purge_enabled: bool
) -> None:
    """Unlike the purge it ships on: it deletes only rows every code path already refuses."""
    built = _built(monkeypatch, guest_purge_enabled=purge_enabled)

    assert _BEAT_ENTRY in built.conf.beat_schedule
    assert built.conf.beat_schedule[_BEAT_ENTRY]["task"] == _SWEEP_TASK


def test_the_sweep_runs_hourly_on_the_default_queue_with_an_expiry_below_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _built(monkeypatch)
    entry = built.conf.beat_schedule[_BEAT_ENTRY]

    assert entry["schedule"] == 3600.0
    assert entry["options"]["queue"] == "celery", (
        "hygiene must never queue behind `mail`, whose rows it is cleaning up after"
    )
    assert 0 < entry["options"]["expires"] < entry["schedule"]


def test_the_sweeps_beat_name_resolves_to_a_registered_task() -> None:
    assert _SWEEP_TASK in tasks_app_module.app.tasks


def test_the_overdue_grace_is_two_sweep_intervals() -> None:
    """`/health/ready`'s `overdue` counts rows expired more than two ticks ago, so a healthy hourly
    sweep (at most one tick behind) reads zero."""
    assert tasks_app_module.IDENTITY_TOKEN_SWEEP_OVERDUE_GRACE_SECONDS == 2 * 3600
    assert (
        tasks_app_module.IDENTITY_TOKEN_SWEEP_OVERDUE_GRACE_SECONDS
        == 2 * tasks_app_module.IDENTITY_TOKEN_SWEEP_INTERVAL_SECONDS
    )
