"""Integration test: the production entry point binds every HTTP seat it owns.

Phase:   F0 / F3
Tasks:   docs/TASKS.md#t-f0-06, docs/TASKS.md#t-f3-11
Covers:  agent_core/main.py (build_app), agent_core/composition.py (Container.turn_lookup,
         Container.decide_approval)

WHY THIS FILE EXISTS
    `t-f0-06` and `t-f3-11` built `GET /turns/{turn_id}` and `POST /decisions/{corr_id}`,
    and their own tests (`tests/integration/test_turn_polling.py`) prove the routes by
    injecting `lookup_turn` / `decide` straight into `routes.create_app`. Nothing bound
    those seats in the PRODUCTION path: `composition.py` builds a real `decide_approval`
    that `main.py` never hands to `create_app`, and no `lookup_turn` adapter existed at
    all. So the process `main.serve()` actually starts answers 503 on BOTH routes forever
    - a `PullModeChannel` nobody can ever read from, and a suspended turn nobody can ever
    approve - while every test written against `routes.py` directly stays green.

    This is the fourth instance of the same shape docs/STATE.md names: a test supplies the
    collaborator production never binds. The fix has to be proved the same way the gap
    hid - by going through `main.build_app()`, exactly as `main.serve()` calls it, with
    NEITHER seat injected. Injecting `lookup_turn` or `decide` here would assert the thing
    this file exists to refuse to assert.

WHY ONLY "NOT 503"
    A freshly migrated, otherwise empty database has no matching turn and no matching
    correlation, so the honest answer for both requests is 404 - and this asserts that
    too. But the property this anchor is about is narrower and must not drown in it: a
    seat that was never bound answers 503 unconditionally, before touching a database at
    all, and no other status code is reachable from that branch. 404 is proof the request
    got PAST the unbound-seat guard and into the real collaborator; it is not the whole of
    what `t-f0-06`/`t-f3-11` still owe.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid

import httpx
import psycopg
import pytest

from agent_core import composition, main

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)

_APP_DATABASE = "agent_core_main_entrypoint_test"


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


def _app_conninfo() -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", rf"/{_APP_DATABASE}\1", _ADMIN_CONNINFO)


def _recreate_empty_database() -> str:
    """A database with nothing in it, exactly as `test_startup_migrations.py` does.

    Dropped and recreated, not truncated: the claim under test is what a caller against a
    FRESH deployment gets, and a leftover row from an earlier run would answer something
    other than "unknown".
    """
    with psycopg.connect(_ADMIN_CONNINFO, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_APP_DATABASE}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_APP_DATABASE}"')
    return _app_conninfo()


def _headers(subject: str = "u-1", tenant: str = "t-1") -> dict[str, str]:
    return {"X-Subject-Id": subject, "X-Tenant-Id": tenant}


@pytest.mark.phase("F0")
@pytest.mark.skipif(not _postgres_reachable(), reason="no reachable Postgres instance")
def test_get_turn_and_post_decision_reach_real_collaborators_through_main_build_app() -> None:
    """docs/TASKS.md#t-f0-06 / docs/TASKS.md#t-f3-11, through the process's own entry point.

    `composition.start_container` is the same call `main.serve()` makes before building
    the app; `container_factory` only substitutes WHICH built container `build_app` reads,
    never a route-level seat - see WHY ONLY "NOT 503" above for why that line must not
    move.
    """
    conninfo = _recreate_empty_database()
    container = asyncio.run(
        composition.start_container(composition.Settings(app_conninfo=conninfo))
    )
    container.domain_pool.open()
    container.audit_pool.open()

    try:
        app = main.build_app(container_factory=lambda: container)
        # `turns.turn_id` is a UUID column (migrations.py) - a non-UUID path segment
        # would fail on the column's TYPE, not on "no such row", and that is a different
        # claim than the one this test makes.
        unknown_turn_id = str(uuid.uuid4())

        async def drive() -> tuple[httpx.Response, httpx.Response]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://agent-core.test"
            ) as client:
                polled = await client.get(
                    f"/turns/{unknown_turn_id}", headers=_headers()
                )
                decided = await client.post(
                    "/decisions/not-a-real-correlation",
                    json={"approved": True},
                    headers=_headers(),
                )
            return polled, decided

        polled, decided = asyncio.run(drive())
    finally:
        container.audit_pool.close()
        container.domain_pool.close()

    assert polled.status_code != 503, (
        f"GET /turns/{{turn_id}} answered 503 through main.build_app(): {polled.text}. "
        "The production entry point never binds a lookup_turn seat, so no real turn is "
        "ever readable back and the http channel's deliberate no-op send reaches nobody."
    )
    assert polled.status_code == 404, (
        "GET /turns/{turn_id} for an id that does not exist in a freshly migrated, empty "
        f"database answered {polled.status_code}, not 404: {polled.text}"
    )

    assert decided.status_code != 503, (
        f"POST /decisions/{{corr_id}} answered 503 through main.build_app(): "
        f"{decided.text}. composition.py builds a real DecideApproval "
        "(container.decide_approval) and main.py never hands it to routes.create_app."
    )
    assert decided.status_code == 404, (
        "POST /decisions/{corr_id} for a correlation nothing published answered "
        f"{decided.status_code}, not 404: {decided.text}"
    )
