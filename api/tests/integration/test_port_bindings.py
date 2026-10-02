"""Port-binding coverage (slice 2.3, T23b, test-after): **a port with no binding is a bug.**

Every `typing.Protocol` under `tailorcraft.domain` — discovered from the package, never hand-listed —
must be bound by a composition root that uses it, or carry an exemption below that says, in a
sentence, where it *is* bound and why that root is not one of the two checked here.

**The API root (`infrastructure/api/deps.py`), behaviourally.** The live application's routes are
walked (2.2's walker, reused). Wherever a dependency provider builds an application use case, each
of that use case's `Protocol`-typed constructor parameters must be supplied by a sibling dependency
whose provider is *typed as that Protocol* — the `get_x(...) -> Port` + `Annotated[Port,
Depends(get_x)]` shape every binding in `deps.py` uses. A use case built with an adapter constructed
inline, or with a port nobody provides, fails by name. The union of those providers is the API's
bound set.

**The worker root (`infrastructure/tasks/container.py`), behaviourally.** Each `_build_*` function —
the same functions the Celery tasks call — is run against the test session, and every
`Protocol`-typed dependency of the use case it returns must be a real adapter (a class defined under
`tailorcraft.infrastructure`), never `None` and never a test double.

**Observed red, 2026-09-29, and restored byte-exact** (`git diff --stat -- src` empty; green after).
Mutation: in `deps.py`, `get_list_tailoring_history` stopped depending on
`TailoringHistoryQueryDep` and built `SqlAlchemyTailoringHistoryQuery(session)` inline — the port
left unbound, the route still working. Three tests went red (3 failed, 3 passed):
```
test_every_use_case_the_api_builds_gets_each_port_from_a_port_typed_provider
E   Left contains one more item: 'get_list_tailoring_history -> ListTailoringHistory: no provider
    for TailoringHistoryQuery'
test_every_domain_port_is_bound_by_a_composition_root_or_exempted
E   AssertionError: unbound ports: ['TailoringHistoryQuery']
test_the_2_3_ports_are_bound_by_the_api[TailoringHistoryQuery]
E   AssertionError: assert <class '…TailoringHistoryQuery'> in {<class '…EventPublisherPort'>, …}
```
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import typing
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from sqlalchemy.ext.asyncio import AsyncSession

import tailorcraft.domain
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container
from tests.api.test_auth_route_dependency_boundary import _iter_api_routes

# Ports bound by a composition root other than the two checked here. Each entry names that root.
_BOUND_ELSEWHERE: dict[str, str] = {
    "OrphanFileScannerPort": (
        "bound only by the operator's `purge-guests --orphans` CLI "
        "(`retention/purge_command.py::_reclaim_orphans`); the sweep is never on beat and never "
        "behind a route (ADR-0018)."
    ),
    # Slice 2.5, the one T11 exemption T22 cannot remove: bound in `deps.py`
    # (`get_account_mail_queue`), but this walker sees only providers a route reaches, and no route
    # depends on `RequestRegistrationDep`/`RequestPasswordResetDep` until T26's skeleton. The stale
    # check below fails the moment one does, so T26 must remove this entry.
    "AccountMailQueuePort": (
        "bound by `deps.get_account_mail_queue`; reached by a route from T26 (remove it then)."
    ),
}


def _domain_protocols() -> dict[str, type]:
    found: dict[str, type] = {}
    for info in pkgutil.walk_packages(tailorcraft.domain.__path__, "tailorcraft.domain."):
        module = importlib.import_module(info.name)
        for name, value in vars(module).items():
            if (
                inspect.isclass(value)
                and getattr(value, "_is_protocol", False)
                and value.__module__ == module.__name__
            ):
                found[name] = value
    return found


_PROTOCOLS = _domain_protocols()
_PROTOCOL_TYPES = set(_PROTOCOLS.values())


def _return_type(call: object) -> type | None:
    try:
        hint = typing.get_type_hints(call).get("return")
    except (NameError, TypeError):
        return None
    return hint if isinstance(hint, type) else None


def _protocol_params(cls: type) -> dict[str, type]:
    hints = typing.get_type_hints(vars(cls)["__init__"])  # the class's own constructor
    return {name: hint for name, hint in hints.items() if hint in _PROTOCOL_TYPES}


def _is_use_case(value: type | None) -> typing.TypeGuard[type]:
    return value is not None and value.__module__.startswith("tailorcraft.application.")


def _walk(dependant: Dependant) -> Iterator[Dependant]:
    stack = [dependant]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.dependencies)


def _api_bindings(app: FastAPI) -> tuple[set[type], list[str]]:
    """The Protocols the API's providers bind, and every use-case construction missing one."""
    bound: set[type] = set()
    problems: set[str] = set()
    for route in _iter_api_routes(app.routes):
        for node in _walk(route.dependant):
            if node.call is None:
                continue
            built = _return_type(node.call)
            if built in _PROTOCOL_TYPES:
                bound.add(built)
            if not _is_use_case(built):
                continue
            provided = {_return_type(sub.call) for sub in node.dependencies}
            for protocol in _protocol_params(built).values():
                if protocol not in provided:
                    name = getattr(node.call, "__name__", repr(node.call))
                    problems.add(f"{name} -> {built.__name__}: no provider for {protocol.__name__}")
    return bound, sorted(problems)


def _worker_use_cases(settings: Settings, session: AsyncSession) -> list[object]:
    purge, _overdue = container._build_purge_use_case(settings, session)
    return [
        container._build_use_case(settings, session),
        container._build_sweep_use_case(settings, session),
        container._build_export_use_case(settings, session),
        container._build_export_sweep_use_case(settings, session),
        purge,
        container._build_registration_delivery(settings, session),
        container._build_password_reset_delivery(settings, session),
        container._build_identity_token_sweep(session),
    ]


def test_ports_are_discovered_from_the_domain_package() -> None:
    """The positive control for discovery: the 2.3 ports are among the Protocols found."""
    assert {"TailoringHistoryQuery", "HistoryEntryDataPort", "LlmPort", "Clock"} <= set(_PROTOCOLS)


def test_every_use_case_the_api_builds_gets_each_port_from_a_port_typed_provider(
    app: FastAPI,
) -> None:
    bound, problems = _api_bindings(app)

    assert problems == []
    assert bound, "the walker found no port-typed provider at all — is it recursing?"


def test_every_use_case_the_worker_builds_holds_a_real_adapter_for_each_port(
    settings: Settings, session: AsyncSession
) -> None:
    problems: list[str] = []
    for use_case in _worker_use_cases(settings, session):
        for name, protocol in _protocol_params(type(use_case)).items():
            adapter = getattr(use_case, f"_{name}", None)
            module = type(adapter).__module__
            if adapter is None or not module.startswith("tailorcraft.infrastructure."):
                problems.append(
                    f"{type(use_case).__name__}.{name} ({protocol.__name__}) is {adapter!r}"
                )

    assert problems == []


def test_every_domain_port_is_bound_by_a_composition_root_or_exempted(
    app: FastAPI, settings: Settings, session: AsyncSession
) -> None:
    api_bound, _ = _api_bindings(app)
    worker_bound = {
        protocol
        for use_case in _worker_use_cases(settings, session)
        for protocol in _protocol_params(type(use_case)).values()
    }
    bound_names = {protocol.__name__ for protocol in api_bound | worker_bound}

    unbound = sorted(set(_PROTOCOLS) - bound_names - set(_BOUND_ELSEWHERE))
    assert not unbound, f"unbound ports: {unbound}"
    stale = sorted(set(_BOUND_ELSEWHERE) & bound_names)
    assert not stale, f"exempted but bound by a checked root — drop the exemption: {stale}"


@pytest.mark.parametrize("port", ["TailoringHistoryQuery", "HistoryEntryDataPort"])
def test_the_2_3_ports_are_bound_by_the_api(app: FastAPI, port: str) -> None:
    api_bound, _ = _api_bindings(app)

    assert _PROTOCOLS[port] in api_bound
