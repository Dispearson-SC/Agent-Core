"""Golden turns of the glazed vertical: one harness for the old build and the addon build (G6).

Not a test module. The same scripted turns run against a `Build`: the tools of a role, the persona,
the policy rules and the audit argument policy of ONE build. `tests/fixtures/glazed_parity/
capture_golden.py` runs them on vertical/glazed (the old, patched Core) and writes `golden.json`;
`tests/unit/test_glazed_golden_turns.py` runs them on the addon build and compares.

The harness imports only what both Cores have, under the same names: `TurnContext`, the turn types,
`RuleSet`, `PgToolPolicy.decide/filter_toolset` and `PgAuditSink`. Everything glazed comes from the
`Build`, so nothing here decides what the answer is.

A TURN, AS CORE RUNS IT
    A `FunctionModel` plays the scripted tool calls one per step. Every call goes through a gate
    that does what Core's `before_tool_execute` does, in its order: the policy decides on the
    VALIDATED arguments, the audit row is written, then a DENY is answered with the rule's reason
    (the tool never runs), NEEDS_APPROVAL suspends the turn and ALLOW runs the tool. The fake
    backend (an `httpx.MockTransport`) answers each request from the scenario's routes.

WHAT IS RECORDED (and compared)
    per scenario: the tools the model is offered after the policy filter, the tool definitions it
    sees (name, description, JSON schema), the persona; per call: the arguments, the policy outcome,
    the audit row as `PgAuditSink` would insert it, every HTTP request (method, path, query,
    headers, body) and the tool result as the model reads it.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import AbstractToolset, WrapperToolset

from agent_core.adapters.driven.persistence_pg.audit_repository import PgAuditSink
from agent_core.adapters.driven.persistence_pg.policy_repository import PgToolPolicy
from agent_core.adapters.driven.tools.context import TurnContext
from agent_core.domain.policy import Effect, PolicyRule, RuleSet
from agent_core.domain.turn import CallerIdentity, SessionRef, TenantId, TurnId

__all__ = ["BASE_URL", "SCENARIOS", "Build", "run_scenarios"]

BASE_URL = "http://backend.test:9"
STORE = "S030"
CASE = "case-77"
PEER_SESSION = f"peer~{CASE}~0f1e"

# The three identities glazed turns run under (addons/glazed_tools/policy/rules.yaml).
MANAGER = {"subject": "manager-1", "channel": "glazed", "roles": ["glazed-manager"]}
SERVICE = {"subject": "glazed-backend", "channel": "glazed", "roles": ["glazed-service"]}
PEER = {"subject": "manager-1", "channel": "peer", "roles": ["peer"]}

_PROBLEM = {"content-type": "application/problem+json"}
# `decide` and `filter_toolset` are pure: the connection factory is never called.
_POLICY = PgToolPolicy(lambda: None)  # type: ignore[arg-type, return-value]

# Each scenario: a role's turn under one identity, the backend's answers by "METHOD /path" (any
# other route answers 200 with `{}`), and the tool calls the model makes, in order.
SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "orchestrator-reads-the-briefing-and-proposes",
        "role": "orchestrator",
        "caller": MANAGER,
        "session": CASE,
        "routes": {
            "GET /internal/v1/briefing": {
                "json": {
                    "headline": [{"fact": "waste up", "value": 12.5, "unit": "pct"}],
                    "suggested_focus": ["I-1"],
                    "snapshot": {"store_display_name": "Centro", "as_of": "2026-03-05"},
                }
            },
            "POST /internal/v1/proposals": {
                "status": 201,
                "json": {
                    "proposal_id": "P-9",
                    "status": "pending",
                    "requires_approval": True,
                    "tier": "N2",
                },
            },
            "POST /internal/v1/notes": {
                "json": {"saved": True, "memory_id": "M-1", "category": "eventos"}
            },
        },
        "calls": [
            ["get_briefing", {}],
            ["propose_action", {"issue_id": "I-1", "option_id": "O-2", "rationale": "Less waste"}],
            ["save_manager_note", {"text": "Feria el sábado", "date_from": "2026-03-07"}],
        ],
    },
    {
        "id": "orchestrator-classifies-a-rejection",
        "role": "orchestrator",
        "caller": MANAGER,
        "session": CASE,
        "routes": {
            "POST /internal/v1/classify": {
                "json": {"label": "price", "confidence": 0.8, "model": "m", "calibrated": True}
            }
        },
        "calls": [
            ["classify_text", {"kind": "rejection_reason", "text": "Es muy caro"}],
            ["classify_text", {"kind": "mood", "text": "x"}],
        ],
    },
    {
        "id": "present-reads-kpis-issues-and-expiring",
        "role": "present",
        "caller": PEER,
        "session": PEER_SESSION,
        "routes": {
            "GET /internal/v1/kpis": {
                "json": {"net_sales": 1200.5, "waste_usd": 40, "store_display_name": "Centro"}
            },
            "GET /internal/v1/issues": {"json": [{"issue_id": "I-1", "severity_usd": 80}]},
            "GET /internal/v1/expiring": {
                "json": {
                    "store_display_name": "Centro",
                    "as_of": "2026-03-05",
                    "store_id": "S030",
                    "days": 3,
                    "total_items": 1,
                    "total_waste_usd": {"low": 1, "mid": 2, "high": 3},
                    "by_day": [{"date": "2026-03-06", "waste_usd": 2, "source": "x"}],
                    "items": [
                        {
                            "sku": "SKU-1",
                            "product_name": "Dona",
                            "expiring_units": 4,
                            "expiry_date": "2026-03-06",
                            "lot_id": "internal",
                        }
                    ],
                }
            },
        },
        "calls": [
            ["get_kpis", {"date_from": "2026-03-01", "date_to": "2026-03-05"}],
            ["get_issues", {"limit": 500, "category": "all"}],
            ["get_issues", {"category": "bogus"}],
            ["get_expiring", {"days": 3}],
        ],
    },
    {
        "id": "past-recalls-with-a-warning-and-meets-a-missing-promo",
        "role": "past",
        "caller": PEER,
        "session": PEER_SESSION,
        "routes": {
            "GET /internal/v1/experiences": {
                "json": [{"text": "combo failed", "decision_id": "D1", "kind": "promo"}],
                "headers": {"X-Glazed-Warning": "memory degraded"},
            },
            "GET /internal/v1/promos/P%2F9/evaluation": {
                "status": 404,
                "json": {"detail": "promo P/9 is not in this case"},
            },
            "GET /internal/v1/history": {"json": {"decisions": [], "events": []}},
        },
        "calls": [
            ["recall_experiences", {"query": "combo PR004", "limit": 3}],
            ["evaluate_promo", {"promo_id": "P/9"}],
            ["get_history", {}],
        ],
    },
    {
        "id": "supply-forecasts-and-meets-an-unavailable-view",
        "role": "supply",
        "caller": PEER,
        "session": PEER_SESSION,
        "routes": {
            "GET /internal/v1/forecast/demand": {
                "json": {
                    "qty_range": {"p10": 3, "p50": 5, "p90": 8},
                    "model": "lgbm",
                    "model_version": "7",
                    "forecast_origin": "2026-03-05",
                    "debug": "dropped",
                }
            },
            "GET /internal/v1/inventory": {
                "status": 503,
                "json": {"title": "Unavailable", "detail": "inventory is being recomputed"},
                "headers": _PROBLEM,
            },
            "GET /internal/v1/inventory/projection": {"json": {"sku": "SKU-1", "days": []}},
        },
        "calls": [
            ["forecast_demand", {"sku": "SKU-1", "target_date": "2026-03-09"}],
            ["get_inventory_status", {"sort": "slow", "limit": 99}],
            ["project_inventory", {"sku": "SKU-1", "horizon": 90}],
        ],
    },
    {
        "id": "strategist-audits-a-promo-and-meets-a-missing-forecast",
        "role": "strategist",
        "caller": PEER,
        "session": PEER_SESSION,
        "routes": {
            "POST /internal/v1/promos/audit": {
                "json": {"verdict": "block", "reasons": ["noise"], "noise_floor_pct": 4}
            },
            "GET /internal/v1/forecast/flow": {
                "status": 404,
                "text": "no traffic model for this store",
                "headers": {"content-type": "text/plain"},
            },
            "GET /internal/v1/staffing": {
                "json": {
                    "benchmark": "tx/h",
                    "note": "daily only",
                    "days": [{"date": "2026-03-06", "recommended_staff_hours": {"mid": 30}}],
                }
            },
        },
        "calls": [
            ["audit_promo", {"department": "bakery", "discount_pct": 25}],
            ["forecast_daily_flow", {"target_date": "2026-03-08"}],
            ["get_staffing", {"days": 3}],
        ],
    },
    {
        "id": "sentinel-records-an-event-and-checks-compliance",
        "role": "sentinel",
        "caller": SERVICE,
        "session": CASE,
        "routes": {
            "POST /internal/v1/events": {"status": 201, "json": {"event_id": "E-1"}},
            "GET /internal/v1/compliance": {
                "json": [{"decision_id": "D-4", "execution_status": "executed"}]
            },
        },
        "calls": [
            [
                "record_event",
                {
                    "type": "holiday",
                    "date_from": "2026-03-10",
                    "date_to": "2026-03-12",
                    "note": "puente",
                },
            ],
            ["check_compliance", {"decision_id": "D-4"}],
        ],
    },
    {
        "id": "auditor-reads-the-integrity-issues",
        "role": "auditor",
        "caller": SERVICE,
        "session": CASE,
        "routes": {
            "GET /internal/v1/issues": {
                "json": [{"issue_id": "X-1", "category": "integrity", "exposure_usd": 300}]
            }
        },
        "calls": [["get_integrity_issues", {}]],
    },
    {
        "id": "liaison-lists-transfers-and-proposes-one",
        "role": "liaison",
        "caller": PEER,
        "session": PEER_SESSION,
        "routes": {
            "GET /internal/v1/transfers/options": {
                "json": {"options": [{"option_id": "T-1", "qty": 6}], "assumptions": {}}
            },
            "POST /internal/v1/transfers/proposals": {
                "status": 201,
                "json": {"proposal_id": "P-10", "status": "pending"},
            },
        },
        "calls": [
            ["get_transfer_options", {"sku": "SKU-1", "limit": 3}],
            ["get_transfer_options", {"sku": None}],
            ["propose_transfer", {"option_id": "T-1", "rationale": "Surplus in Norte"}],
        ],
    },
    {
        "id": "a-peer-turn-may-not-propose",
        "role": "orchestrator",
        "caller": PEER,
        "session": PEER_SESSION,
        "routes": {},
        "calls": [
            ["propose_action", {"issue_id": "I-1", "option_id": "O-2", "rationale": "x"}],
            ["get_briefing", {}],
        ],
    },
]


@dataclass(frozen=True)
class Build:
    """One build of the glazed vertical, as the harness needs it."""

    toolset: Callable[[str], AbstractToolset[Any]]  # role -> a fresh toolset of that role
    persona: Callable[[str], str]  # role -> the persona the model is given
    rules: tuple[PolicyRule, ...]
    # (by value, by presence) as the build hands them to `PgAuditSink`; None = the sink's defaults
    audit_policy: tuple[Mapping[str, frozenset[str]], Mapping[str, frozenset[str]]] | None
    install_transport: Callable[[httpx.AsyncBaseTransport | None], None]


class _Backend:
    def __init__(self, routes: Mapping[str, Mapping[str, Any]]) -> None:
        self.routes, self.requests = routes, []  # type: ignore[var-annotated]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(_request(request))
        path = request.url.raw_path.decode().split("?", 1)[0]
        spec = self.routes.get(f"{request.method} {path}", {})
        status = int(spec.get("status", 200))
        headers = dict(spec.get("headers", {}))
        if "text" in spec:
            return httpx.Response(status, text=spec["text"], headers=headers)
        if headers.get("content-type", "").startswith("application/problem+json"):
            return httpx.Response(status, content=json.dumps(spec["json"]), headers=headers)
        return httpx.Response(status, json=spec.get("json", {}), headers=headers)


def without_client_version(headers: dict[str, str]) -> dict[str, str]:
    """The headers with httpx's own `user-agent` (`python-httpx/<version>`) as `python-httpx`: the
    version is the environment's, so a capture must not pin it (slice 31 advisory)."""
    agent = headers.get("user-agent", "")
    if not agent.startswith("python-httpx/"):
        return headers
    return {**headers, "user-agent": "python-httpx"}


def _request(request: httpx.Request) -> dict[str, Any]:
    raw = request.content.decode() if request.content else ""
    return {
        "method": request.method,
        "path": request.url.raw_path.decode().split("?", 1)[0],
        "query": [list(pair) for pair in request.url.params.multi_items()],
        "headers": without_client_version(
            dict(sorted((k.lower(), v) for k, v in request.headers.items()))
        ),
        "body": raw,
        "json": json.loads(raw) if raw else None,
    }


class _Audit(PgAuditSink):
    """`PgAuditSink` with its INSERT captured instead of executed."""

    rows: list[dict[str, Any]]

    @classmethod
    def of(cls, build: Build) -> _Audit:
        if build.audit_policy is None:
            sink = cls(lambda: None)  # type: ignore[arg-type, return-value]
        else:
            allow, presence = build.audit_policy
            sink = cls(
                lambda: None,  # type: ignore[arg-type, return-value]
                argument_allowlist=allow,
                argument_presence_only=presence,
            )
        sink.rows = []
        return sink

    async def _append(self, sql: str, params: Any) -> None:
        match = re.search(r"INSERT INTO (\w+) \(([^)]*)\)", sql)
        assert match is not None, sql
        columns = [c.strip() for c in match.group(2).split(",")]
        row = {c: getattr(v, "obj", v) for c, v in zip(columns, params, strict=True)}
        # S8 (migration 0048): `on_behalf_of` postdates the oracle and is NULL for every turn the
        # golden set plays (a person's or a peer's own); a delegated row would keep it and differ.
        if row.get("on_behalf_of", "kept") is None:
            del row["on_behalf_of"]
        self.rows.append({"table": match.group(1), **row})


class _Gate(WrapperToolset[Any]):
    """What Core's `before_tool_execute` does: decide, audit, then refuse, suspend or run."""

    def __init__(
        self, wrapped: AbstractToolset[Any], rules: RuleSet, audit: PgAuditSink, step: list[Any]
    ) -> None:
        super().__init__(wrapped)
        self.rules, self.audit, self.step = rules, audit, step

    async def call_tool(self, name: str, tool_args: dict[str, Any], ctx: Any, tool: Any) -> Any:
        record = self.step[-1]
        decision = _POLICY.decide(self.rules, name, dict(tool_args))
        record["validated_args"] = _plain(tool_args)
        record["policy"] = {
            "effect": decision.effect.value,
            "rule_id": decision.rule_id,
            "reason": decision.reason,
        }
        await self.audit.record_tool_call(
            TurnId("turn-1"), ctx.deps.caller, name, dict(tool_args), decision
        )
        if decision.effect is Effect.NEEDS_APPROVAL:
            record["suspended"] = True
            raise _Suspended
        if decision.blocks_execution:
            return decision.reason
        return await super().call_tool(name, tool_args, ctx, tool)


class _Suspended(Exception):
    """The turn waits for a person (NEEDS_APPROVAL): the scripted turn ends here."""


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _caller(spec: Mapping[str, Any]) -> CallerIdentity:
    return CallerIdentity(
        spec["subject"], spec["channel"], TenantId(STORE), frozenset(spec["roles"])
    )


def _seen(part: Any) -> dict[str, Any]:
    if isinstance(part, ToolReturnPart):
        return {"tool_return": part.model_response_str()}
    if isinstance(part, RetryPromptPart):
        return {"retry": part.model_response()}
    return {"other": type(part).__name__}


def run_scenario(build: Build, scenario: Mapping[str, Any]) -> dict[str, Any]:
    backend = _Backend(scenario["routes"])
    build.install_transport(httpx.MockTransport(backend))
    caller = _caller(scenario["caller"])
    rules = RuleSet.for_caller(caller, build.rules)
    audit = _Audit.of(build)
    steps: list[dict[str, Any]] = []
    offered: dict[str, Any] = {}
    calls = scenario["calls"]

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not offered:
            offered["definitions"] = sorted(
                (
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters_json_schema,
                    }
                    for tool in info.function_tools
                ),
                key=lambda d: d["name"],
            )
        last = messages[-1]
        if isinstance(last, ModelRequest) and steps:
            seen = [_seen(p) for p in last.parts if isinstance(p, ToolReturnPart | RetryPromptPart)]
            steps[-1]["seen_by_model"] = seen
        index = len(steps)
        if index >= len(calls):
            return ModelResponse(parts=[TextPart("done")])
        name, args = calls[index]
        steps.append({"tool": name, "args": args, "requests_before": len(backend.requests)})
        return ModelResponse(parts=[ToolCallPart(name, dict(args), tool_call_id=f"call-{index}")])

    toolset = build.toolset(scenario["role"])
    agent: Agent[TurnContext, str] = Agent(
        FunctionModel(model), deps_type=TurnContext, toolsets=[_Gate(toolset, rules, audit, steps)]
    )
    deps = TurnContext(
        caller=caller,
        session=SessionRef(
            session_id=scenario["session"],
            tenant_id=TenantId(STORE),
        ),
        agent_id=f"glazed_{scenario['role']}",
    )
    try:
        asyncio.run(agent.run("scripted turn", deps=deps))
        suspended = False
    except _Suspended:
        suspended = True
    finally:
        build.install_transport(None)

    names = tuple(d["name"] for d in offered.get("definitions", []))
    for number, step in enumerate(steps):
        end = steps[number + 1]["requests_before"] if number + 1 < len(steps) else None
        step["requests"] = backend.requests[step.pop("requests_before") : end]
    return {
        "role": scenario["role"],
        "persona": build.persona(scenario["role"]),
        "tools": list(names),
        "offered_after_policy": list(_POLICY.filter_toolset(rules, names)),
        "definitions": offered.get("definitions", []),
        "steps": steps,
        "audit": [_plain(row) for row in audit.rows],
        "suspended": suspended,
    }


def run_scenarios(build: Build) -> dict[str, dict[str, Any]]:
    return {s["id"]: run_scenario(build, s) for s in SCENARIOS}
