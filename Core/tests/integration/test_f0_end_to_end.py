"""F0's done criterion against a REAL provider, a REAL database and the REAL runner.

Phase:   F0 (the criterion) / F1 (the runner that now carries it)
Tasks:   docs/TASKS.md#t-f0-04 (the LiteLLM gateway), docs/TASKS.md#t-f1-12 (the runner)
Covers:  adapters/driven/agent_pydantic/runner.py (PydanticAgentRunner + PolicyEnforcement)
         adapters/driven/llm_litellm/gateway.py
         adapters/driven/persistence_pg/policy_repository.py
         adapters/driven/persistence_pg/audit_repository.py

WHAT F0 SAYS IT IS DONE-WHEN, AND WHAT THIS FILE ACTUALLY PROVES
    The F0 header reads "a POST returns a model response that used the tool, with the
    `tool_call` in the log". The POST half of that sentence cannot be true and is not a
    goal: `POST /turns` answers 202 and never waits for the turn - that is the whole
    reason the route exists (adapters/driving/http/routes.py, t-f0-03). The half that
    matters and IS provable today is the rest of it: a real model, given a real tool,
    calls it; the call passes the real policy gate; the real audit sink writes the
    `tool_call` row to Postgres; the tool really runs; and the model's final answer
    carries the tool's result back.

    So this module proves the criterion below the route, in this order of importance:

      a. the model CALLED the tool - a structured tool_call, not a sentence about one;
      b. the tool EXECUTED - it leaves a file on disk, which a no-op cannot fake;
      c. the `tool_call` row landed in `audit_tool_calls` with the tool name AND the
         policy decision's `rule_id` - the field that answers "why was this allowed?";
      d. the model's final response reflects what the tool returned.

    THE RUNNER IS BUILT THE WAY PRODUCTION BUILDS IT. No `model_factory` is injected:
    the model comes from `runner.litellm_model_factory`, which is the default and is now
    production's real path to a provider. That is the point of this revision - see below.

THE BORROWED TOOL LOOP IS GONE, AND SO IS THE BORROWED MODEL FACTORY
    This module used to drive the two-step tool loop - call the model, gate the call, run
    the tool, hand the result back - IN ITS OWN TEST BODY, because
    `PydanticAgentRunner.__init__` raised `NotImplementedError` and there was nothing else
    to drive. That was repaid by t-f1-12: the loop below is the runner's.

    It then still injected its own `ModelFactory`, for a reason that sounded local and was
    not: day-1 library mode had NO endpoint, so `litellm_model_factory` refused and only a
    test carrying its own factory could reach a provider. An integration test that has to
    supply the one component production cannot build is not proving the production path -
    it is standing in for it, and it hides the fact that production has no path at all.

    The endpoint exists now (adapters/driven/llm_litellm/models.py), so the factory is gone
    too. If this file ever needs one again, that is the signal the gap came back.

WHAT WENT WITH THE FACTORY: THE `reasoning_effort` PARAMETER
    This module used to be parametrized over MiniMax M3's two modes, default and
    `reasoning_effort="high"`, which it could only do by injecting a factory carrying
    `OpenAIChatModelSettings`. `ModelFactory` is `(model_id, base_url) -> Model` and
    `AgentProfile` has no model-settings field, so there is no seat for a per-run model
    setting between a profile and the runner today - and inventing one HERE would put a
    profile's limits somewhere other than the profile.

    So the parametrization is gone rather than faked, the seat is named as a gap in
    docs/TASKS.md, and what M3 actually does in both modes is recorded once in
    docs/FIELD-NOTES.md: reasoning tokens come back in BOTH, so no assertion anywhere
    should claim one reasons and the other does not. Losing it also halves the paid calls.

WHY THIS STILL DOES NOT DRIVE `StartTurn`
    `StartTurn` takes eight collaborators and three still have no constructible adapter in
    the tree - `ToolProvider` (docs/TASKS.md#t-f1-07, first real one F4), `ContextEngine`
    (F5) and `SkillRegistry` (F6) - and `PgConversationStore.append_outcome` is itself
    still pending (t-f1-13). `composition.py` says the same thing out loud and still
    refuses to wire the use case. The tiny `_DeliveryToolProvider` below is a TEST double
    for the one seat this file needs, not an adapter: it exists in the test tree precisely
    so nobody mistakes it for the F4 vertical.

COST
    Two provider calls: one that answers with a tool call, one that answers with the code.
    Tiny prompt, and the run is bounded by the profile's own `max_iterations` and
    `max_cost_usd` rather than by a cap this file sets - a test that configures its own
    ceilings is configuring something production does not have. This is a paid API; keep
    it that way.

SKIP GUARD
    Skips cleanly - with a reason `-rs` prints - when no MiniMax credential is available
    or no Postgres is reachable, mirroring tests/integration/test_migrations.py. CI
    without a key must not fail.

THE CREDENTIAL
    The repository's gitignored `.env` stores it under `MINIMAX_API`; LiteLLM's own name
    for it is `MINIMAX_API_KEY`, and litellm is what resolves it now that the adapter -
    not this file - builds the client. So the mapping is done by putting the value into
    the environment under LiteLLM's name, with `monkeypatch`, for the duration of the one
    test that needs it. The value is never asserted on, never formatted into a message and
    never written anywhere; `monkeypatch` also guarantees it does not outlive the test.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic_ai.toolsets import FunctionToolset

# Imported as a MODULE, like tests/unit/test_runner_hooks.py: the file still holds targets
# that belong to later phases, and a name-level import would turn "not implemented yet"
# into a collection-time error instead of a red assertion inside a test that ran.
from agent_core.adapters.driven.agent_pydantic import runner as runner_module
from agent_core.adapters.driven.llm_litellm.gateway import LiteLLMGateway
from agent_core.adapters.driven.persistence_pg import migrations
from agent_core.adapters.driven.persistence_pg.audit_repository import PgAuditSink
from agent_core.adapters.driven.persistence_pg.policy_repository import PgToolPolicy
from agent_core.domain.policy import Effect
from agent_core.domain.profile import AgentProfile
from agent_core.domain.turn import (
    CallerIdentity,
    SessionRef,
    TenantId,
    TurnId,
    TurnRequest,
    UserInput,
)
from agent_core.ports.agent_runner import AgentRunner

pytestmark = [pytest.mark.phase("F0")]

_ADMIN_CONNINFO = os.environ.get(
    "AGENT_CORE_TEST_ADMIN_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)
_APP_DATABASE = "agent_core_f0_test"

# The model string is handed to `LiteLLMGateway.model_id_for`, never to a client directly,
# so the F0 adapter is genuinely in the loop rather than decorative.
_PROFILE_MODEL = "minimax/MiniMax-M3"

_TOOL_NAME = "lookup_delivery_code"
_RULE_ID = "r-f0-delivery-lookup"
_ORDER_ID = "ord-42"

_PERSONA = (
    "You are a delivery desk assistant. Use the provided tool to answer. "
    "Once you have the delivery code, reply with that code and nothing else."
)


def _repo_root() -> Path:
    """`Core/tests/integration/this_file.py` -> the repository root, three parents up."""
    return Path(__file__).resolve().parents[3]


def _minimax_api_key() -> str | None:
    """The provider credential, or None when there is none to be had.

    `MINIMAX_API_KEY` (LiteLLM's name) wins if it is already set. Otherwise the
    gitignored `.env` at the repository root is read for `MINIMAX_API`, which is the name
    the repository stores it under. The return value is a secret: it goes into the model
    client and into no assertion, message or file.
    """
    from_environment = os.environ.get("MINIMAX_API_KEY")
    if from_environment:
        return from_environment

    env_file = _repo_root() / ".env"
    if not env_file.exists():
        return None

    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "MINIMAX_API":
            return value.strip().strip('"').strip("'") or None
    return None


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(_ADMIN_CONNINFO, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


_needs_model = pytest.mark.skipif(
    _minimax_api_key() is None,
    reason="no MiniMax credential: set MINIMAX_API_KEY, or MINIMAX_API in the repo .env",
)
_needs_postgres = pytest.mark.skipif(
    not _postgres_reachable(), reason="no reachable Postgres instance"
)


def _caller() -> CallerIdentity:
    return CallerIdentity(
        subject_id="u-f0",
        channel="http",
        tenant_id=TenantId("t-f0"),
        roles=frozenset({"operator"}),
    )


class DeliveryDesk:
    """The one real tool, and its side effect is a file.

    A tool that only returns a string cannot tell "the model called it" apart from "the
    test called it": both leave the same trace, which is none. Writing the order id to
    disk makes assertion (b) a fact about the world rather than about this process.
    """

    def __init__(self, log_path: Path, delivery_code: str) -> None:
        self.log_path = log_path
        self.delivery_code = delivery_code
        self.calls: list[str] = []

    def lookup_delivery_code(self, order_id: str) -> str:
        """Return the internal delivery code for one order id."""
        self.calls.append(order_id)
        self.log_path.write_text(f"dispatched {order_id}\n", encoding="utf-8")
        return self.delivery_code


class _DeliveryToolProvider:
    """A TEST double for `ToolProvider`, not an adapter. See WHY THIS STILL DOES NOT DRIVE
    `StartTurn` in the module docstring: the F1 local provider is docs/TASKS.md#t-f1-07 and
    the first real one is the F4 vertical. Living here keeps that distinction visible."""

    def __init__(self, desk: DeliveryDesk) -> None:
        self._toolset = FunctionToolset([desk.lookup_delivery_code])

    async def toolset_for(self, profile: AgentProfile) -> object:
        return self._toolset

    async def tool_names_for(self, profile: AgentProfile) -> tuple[str, ...]:
        return (_TOOL_NAME,)


@pytest.fixture(scope="module")
def app_conninfo() -> str:
    """A migrated database of this module's own. Created once, reused by both parameters."""
    asyncio.run(
        migrations.ensure_databases(
            _ADMIN_CONNINFO,
            app_database=_APP_DATABASE,
            dbos_database=f"{_APP_DATABASE}_dbos",
        )
    )
    conninfo = re.sub(r"/[^/?]+(\?.*)?$", rf"/{_APP_DATABASE}\1", _ADMIN_CONNINFO)
    asyncio.run(migrations.run_migrations(conninfo))
    return conninfo


@pytest.fixture(scope="module")
def seeded_policy(app_conninfo: str) -> str:
    """One real ALLOW rule in the real `policy_rules` table.

    The rule is the source of the `rule_id` assertion (c). It is stored the way migration
    0004 shapes the table - every field inside `definition` jsonb - and carries no
    `tenant_id`, which `PgToolPolicy` reads as "all tenants".
    """
    definition: dict[str, Any] = {
        "tool_pattern": _TOOL_NAME,
        "effect": Effect.ALLOW.value,
        "reason": "the delivery desk may read a delivery code",
        "subject_roles": ["operator"],
        "channels": ["http"],
    }
    with psycopg.connect(app_conninfo, autocommit=True) as connection:
        connection.execute("DELETE FROM policy_rules WHERE rule_id = %s", (_RULE_ID,))
        connection.execute(
            "INSERT INTO policy_rules (rule_id, definition) VALUES (%s, %s)",
            (_RULE_ID, Jsonb(definition)),
        )
    return app_conninfo


@_needs_model
@_needs_postgres
def test_a_real_model_uses_a_real_tool_and_the_tool_call_lands_in_the_audit_log(
    seeded_policy: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F0's criterion, minus the POST: assertions (a) to (d), driven by the real runner
    and the real default model factory - nothing about the model is injected here."""
    api_key = _minimax_api_key()
    assert api_key is not None, "the skip guard should have prevented this"

    # THE NAME MAPPING, AND THE ONLY THING THIS TEST DOES ABOUT THE CREDENTIAL. The adapter
    # asks litellm for the key, and litellm's name for it is MINIMAX_API_KEY; the
    # repository stores it as MINIMAX_API. `monkeypatch` scopes the value to this test and
    # removes it afterwards, so it never leaks into another test's environment.
    monkeypatch.setenv("MINIMAX_API_KEY", api_key)

    # The F0 adapter under test. Day 1 is library mode, so `base_url()` is None and that
    # is a property worth stating rather than assuming.
    gateway = LiteLLMGateway()
    assert gateway.base_url() is None, "day 1 is library mode; a proxy URL is day 2"

    turn_id = TurnId(str(uuid.uuid4()))
    # Fresh per run, so a model that answered correctly cannot have memorised the code and
    # assertion (d) can only pass by carrying the tool's actual return value.
    delivery_code = f"DLV-{uuid.uuid4().hex[:8].upper()}"
    desk = DeliveryDesk(tmp_path / "dispatch.log", delivery_code)

    policy = PgToolPolicy(lambda: psycopg.connect(seeded_policy))
    rules = asyncio.run(policy.load_rules(_caller()))
    assert rules.rules, (
        "the seeded rule did not load; PgToolPolicy fails closed, so an empty snapshot "
        "would deny the tool and this test would prove nothing about the model"
    )

    audit = PgAuditSink(
        lambda: psycopg.connect(seeded_policy, autocommit=True),
        argument_allowlist={_TOOL_NAME: frozenset({"order_id"})},
    )

    profile = AgentProfile.from_mapping(
        {
            "id": "delivery-desk",
            "persona": _PERSONA,
            "model": _PROFILE_MODEL,
            "max_iterations": 6,
            "max_cost_usd": "0.50",
        }
    )

    # The id is GENERATED BY THE CALLER and handed to `run`, never invented by the runner.
    # CLAUDE.md non-negotiable #2.
    runner = runner_module.PydanticAgentRunner(
        model=gateway,
        policy=policy,
        audit=audit,
        tools=_DeliveryToolProvider(desk),
    )

    request = TurnRequest(
        session=SessionRef(session_id="s-f0", tenant_id=TenantId("t-f0")),  # type: ignore[arg-type]
        caller=_caller(),
        profile_id=profile.id,
        input=UserInput(text=f"What is the delivery code for order {_ORDER_ID}?"),
    )

    outcome = asyncio.run(runner.run(turn_id, request, profile, None))

    # The runner returned the turn it was GIVEN, not one of its own making.
    assert outcome.turn_id == turn_id
    assert not outcome.is_suspended, "F1 has no deferred tools; nothing can suspend yet"
    assert outcome.result is not None

    # (b) THE TOOL EXECUTED. The file is the part a no-op cannot fake.
    assert desk.calls == [_ORDER_ID]
    assert desk.log_path.read_text(encoding="utf-8") == f"dispatched {_ORDER_ID}\n"

    # (a) and (c) THE MODEL CALLED THE TOOL, AND THE ROW IS IN THE AUDIT TABLE.
    # `before_tool_execute` only fires on a structured tool call with validated arguments,
    # so the row below IS the evidence that the model emitted a tool_call rather than prose
    # mentioning one - and it carries the rule_id that answers "why was this allowed?".
    with psycopg.connect(seeded_policy) as connection:
        rows = connection.execute(
            "SELECT tool, effect, rule_id, arguments FROM audit_tool_calls WHERE turn_id = %s",
            (str(turn_id),),
        ).fetchall()

    assert len(rows) == 1, "the tool call left no audit row, or left more than one"
    assert rows[0] == (_TOOL_NAME, Effect.ALLOW.value, _RULE_ID, {"order_id": _ORDER_ID})

    # (d) THE FINAL RESPONSE REFLECTS THE TOOL'S RESULT.
    #
    # This is also the pairing assertion. The tool call and its return travel back to the
    # provider as a PAIR, under the id Pydantic AI issued; a broken or re-cased pair is
    # rejected by the provider as a 400 and there is no final answer at all. CLAUDE.md
    # non-negotiable #5 and the silent-bug table.
    final_text = outcome.result.text
    assert delivery_code in final_text, (
        f"the final answer does not carry the code the tool returned: {final_text!r}"
    )

    # The turn was accounted for. A run that reports nothing spent cannot feed ContextEngine
    # or the turn_end audit record.
    assert outcome.result.usage.input_tokens > 0
    assert outcome.result.usage.output_tokens > 0


def test_the_runner_exists_and_no_longer_blocks_start_turn() -> None:
    """The successor to the pin this file used to carry.

    It used to assert `PydanticAgentRunner()` raised `NotImplementedError`, so that the day
    t-f1-12 landed this module failed and got rewritten. It landed. What is worth pinning
    now is the property the rewrite depends on: the runner is CONSTRUCTIBLE with production
    collaborators only, and it needs no per-turn pre-binding to be usable - the turn id is
    a parameter of `run`.

    No model, no database, no cost.
    """
    runner = runner_module.PydanticAgentRunner(
        model=LiteLLMGateway(),
        policy=PgToolPolicy(lambda: psycopg.connect(_ADMIN_CONNINFO)),
        audit=PgAuditSink(lambda: psycopg.connect(_ADMIN_CONNINFO, autocommit=True)),
    )

    port: AgentRunner = runner
    assert port is not None
    assert not hasattr(runner, "for_turn"), (
        "the turn-id pre-binding is back; the id belongs on the call - t-f1-05"
    )
