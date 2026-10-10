# Vertical: Glazed store copilot

Eight agents for one store, following the contract "one profile per agent plus tools
packages plus policy rows" with no change to `domain/`, `application/` or `ports/`.
Code computes (the Glazed backend), agents explain.

## Agents and tools

| Profile | Toolset package | Tools |
|---|---|---|
| `glazed_orchestrator` | `glazed_orchestrator` (+ `peers`, added by the `peers:` block) | `propose_action`, `ask_peer` |
| `glazed_present` | `glazed_present` | `get_kpis`, `get_day_summary`, `get_issues`, `explain_metric` |
| `glazed_past` | `glazed_past` | `get_history`, `evaluate_promo`, `recall_experiences` |
| `glazed_supply` | `glazed_supply` | `get_order_plan`, `project_inventory`, `get_supplier_performance`, `get_issues` |
| `glazed_strategist` | `glazed_strategist` | `get_kpis`, `get_day_summary`, `evaluate_promo` |
| `glazed_sentinel` | `glazed_sentinel` | `get_history`, `record_event` |
| `glazed_auditor` | `glazed_auditor` | `get_issues` (integrity kinds; the backend may return none) |
| `glazed_liaison` | `glazed_liaison` | `offer_surplus`, `request_stock` (placeholders: "not available yet") |

One toolset package per role because the contract test requires a profile to select exactly
one package. Shared code lives in `adapters/driven/tools/glazed/` (`client.py`, `tools.py`);
the `glazed_<role>/tools.py` modules only expose `build_toolset`, registered in
`composition.TOOL_PACKAGES`. Model: `minimax/MiniMax-M3.1-flash-preview` (see Model selection). Budgets are small
(`max_iterations` 5-15, `max_cost_usd` 0.10-0.50 per turn).

`recall_experiences(query, limit=5)` reads the store's long-term memory (Backboard). Its
result is `{"experiences": [...], "warning"?}`; a `warning` (from the backend's
`X-Glazed-Warning` header) means memory is unavailable. The `glazed_past` persona requires
re-verifying any recalled number with `get_history` / `evaluate_promo` (via `decision_id`)
before citing it, and falling back to history only when memory is unavailable.

## Who can ask whom (star topology)

| From \ To | Orchestrator | Specialists | Others |
|---|---|---|---|
| Orchestrator | - | yes (present, past, supply, strategist, sentinel, liaison) | - |
| Specialists | answer only | no | no |
| Auditor | no peers (scheduled; escalates via alert in the backend) | no | no |

Enforced three ways: (1) the orchestrator's `peers:` allowlist; (2) every specialist names
**only** the orchestrator back, because the Core checks the allowlist on both sides, with
`max_hops: 1` so the answering turn (hop 1) is refused if it tries to ask; (3) policy rows
deny `ask_peer` on the `peer` channel. Specialists therefore do carry `ask_peer` in their
resolved toolset (the Core appends it when `peers.enabled`), but it can never succeed.

## Identities and policy (`Core/policy/rules.yaml`, "Glazed" section)

| Identity | Channel | Role | Used for |
|---|---|---|---|
| Manager turn | `glazed` | `glazed-manager` | orchestrator: `propose_action`, `ask_peer` allowed; no data tools |
| Service turn | `glazed` | `glazed-service` | scheduled sentinel / auditor turns: read tools + `record_event` |
| Peer turn | `peer` | `peer` (set by the Core worker) | specialists answering: read tools + `record_event` |

Rules cannot name a profile, so the *profile's* `toolsets` decide which tools an agent holds
and the rules narrow by identity. `propose_action` only creates a pending proposal; nothing
is ever executed by an agent. A backend calling `POST /turns` must send
`X-Channel: glazed`, `X-Roles: glazed-manager` (or `glazed-service`), `X-Tenant-Id: <store_id>`,
`X-Subject-Id: <manager>` and use the **case id as `session_id`**.

## How case, store and as_of are bound (not LLM parameters)

No tool declares `store_id`, `case`, or `as_of`. The runner passes the turn's identity as
Pydantic AI deps (`TurnContext`: caller, session, running profile id; `runner.py`
`deps=TurnContext(...)`), and `glazed/client.py` derives the headers:

| Header | Source |
|---|---|
| `X-Glazed-Case` | session id; for a specialist's peer turn (`peer~<asker session id>~<uuid>`, built by the peer worker from the claimed row) the asking session id, i.e. the case |
| `X-Glazed-Store` | `caller.tenant_id` (= `X-Tenant-Id` = store id) |
| `X-Glazed-Agent` | running profile id (`glazed_supply`, ...) |

The backend resolves store and `as_of` from the case; the tools never send them. Case ids
(= Core session ids) must not contain `~`. Date arguments the model passes (`date_from`,
`date_to`, `date`) are ranges: the backend should clamp them to the case's `as_of`.

## Backend calls (base URL `GLAZED_BACKEND_URL`, default `http://backend:8080`)

`get_kpis` GET `/internal/v1/kpis?date_from&date_to`; `get_day_summary` GET `/day-summary?date`;
`get_issues` GET `/issues?limit`; `explain_metric` GET `/metrics/explain?metric` (not in the
original contract); `get_order_plan` GET `/order-plan`; `project_inventory` GET
`/inventory/projection?sku&horizon`; `get_supplier_performance` GET `/suppliers/performance`;
`get_history` GET `/history`; `evaluate_promo` GET `/promos/{promo_id}/evaluation`;
`recall_experiences` GET `/experiences?query&limit` (plain JSON list; `[]` plus
`X-Glazed-Warning` when memory is degraded); `record_event` POST `/events`
`{type,date_from,date_to,note}`; `propose_action` POST `/proposals`
`{issue_id,option_id,rationale}`. Timeouts: 3 s connect, 10 s total. HTTP
and network failures come back as `{"error": ...}` the agent can explain; they never raise.

## Backend contract the tools rely on (Wave 5)

The prompts and docstrings promise only what the backend returns; anything else is "not
available", never estimated by the LLM.

| Tool | Returns |
|---|---|
| `get_issues` | `Issue { issue_id, kind, sku?, supplier_id?, store_display_name, as_of, severity_usd, evidence[{metric,value,unit,period,source}], data_caveats[], options[] }` |
| (option) | `Option { option_id, action_type, params, expected_benefit_usd{low,mid,high}, cost_usd, net_usd_mid, loss_impact{waste_usd_delta,lost_sales_usd_delta}, confidence{score,n_backtest,method}, tier N0-N3, tier_reasons[], urgency{deadline_date?,reason}, risk[], if_act, if_not }`; up to 4 per issue, always including `do_nothing` with its cost |
| `get_order_plan` | per line: `service_level_target`, `order_deadline`, `projected_stockout_date`, `stockout_risk_without_order`, `vs_current_practice{received_to_sold,qty_delta}`, `min_order_warning` |
| `get_kpis` | `waste_usd` and `lost_sales_usd {low,mid,high}` |

Optional fields may be absent; the docstrings say "may be absent; never estimate it
yourself". Tool results carry `store_display_name`; personas refer to the store by it,
never by code.

**`propose_action(issue_id, option_id, rationale)`** sends only those three fields. The
LLM no longer supplies `action_type`, `params` or `tier`: the backend copies them from the
stored option (`option_id` is a deterministic hash of issue_id + action_type + params), so an
agent can neither alter an action nor under-state its tier.

Personas enforce: every number verbatim from a tool result; options only from
`Issue.options` (including `do_nothing`); recommendation justified with `net_usd_mid`,
`loss_impact`, `confidence.score`, `tier`, `urgency` plus an if-act / if-not narrative;
confidence and tier always stated, low confidence flagged. The Sentinel and Auditor say what
their thin tools do today and nothing more. `tests/unit/test_glazed_prompts.py` scans all
eight profiles for these rules and checks the docstrings.

## Environment

| Variable | Purpose |
|---|---|
| `GLAZED_BACKEND_URL` | Backend internal API base (default `http://backend:8080`) |
| `MINIMAX_API_KEY` | MiniMax credential, the name litellm reads (documented, wins when both are set). `MINIMAX_API` (this repository's `.env` name) is accepted too, in preflight and in the model client. Names only are ever reported, never values |
| `MINIMAX_API_BASE` | Optional MiniMax endpoint override |
| `GEMINI_API_KEY` / `GEMINI_API_BASE` | Gemini credential and optional base override (default: Google's OpenAI-compatible endpoint). See the caveat below |
| `AGENT_CORE_LITELLM_BASE_URL` | Optional LiteLLM proxy; when set, provider credentials are the proxy's |

## Model selection

The Core has **no env or config override** for a profile's model: `model:` in the profile is
the single source of truth. Default for this vertical: `minimax/MiniMax-M3.1-flash-preview`
(chat accepts it although `/models` does not list it; `minimax/MiniMax-M3` also works). To
change all eight profiles at once:

```
python Core/scripts/set_glazed_model.py minimax/MiniMax-M3
```

(or edit the single `model:` line of each `Core/profiles/glazed_*.yaml`). The script rewrites
only the top-level `model:` line and is idempotent.

**Gemini is not supported for this vertical's tool-calling flows.** Library-mode preflight now
resolves `gemini/*` (litellm's Gemini config has no `get_api_base`/`get_api_key`, so the Core
falls back to `GEMINI_API_KEY` and Google's OpenAI-compatible endpoint), but multi-step tool
calls through that endpoint fail with HTTP 400 "Function call is missing a thought_signature".
Use MiniMax.

## Failure behaviour

Every glazed tool returns `{"error": ...}` instead of raising (timeouts, HTTP errors, non-JSON
bodies, unserialisable arguments, any other exception): a tool that raises would leave the
turn stranded as running. `ask_peer` needs the exact peer id as `target`; the orchestrator
persona lists them. The allow row for it is `glazed-orchestrator-asks-specialists` (channel
`glazed`, role `glazed-manager`).

## Running the console against the orchestrator

```
python -m agent_core preflight
python -m agent_core peer-worker                                 # terminal 1
python -m agent_core console --durable --profile glazed_orchestrator \
    --tenant S030 --channel glazed --role glazed-manager --subject manager-1 \
    --session <case_id>                                          # terminal 2
```

`--durable` is required for delegation (see CONSOLE.md). The backend must be reachable at
`GLAZED_BACKEND_URL` and know the case id used as `--session`.
