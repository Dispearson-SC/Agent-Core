# Writing an addon

An addon is how a vertical adds to Agent-Core without patching it: tools, policy, audit entries,
prompt text, profiles, channels, tool wrappers and database tables, all declared in one manifest.
This is the authoring guide. The operator's side (variables, startup errors) is
[OPERATIONS.md](OPERATIONS.md#addons-enabling-them-and-setting-them-phase-2-d70-d72); the design is
[ARCHITECTURE.md](ARCHITECTURE.md#addons-phase-2); the decisions are D70-D90 in
[DECISIONS.md](DECISIONS.md). The shipped samples in `addons/sample_*` are the worked examples and
are loaded by the contract tests, so they cannot drift from the rules below.

## The rules in four lines

1. **Explicit.** An addon runs only if it is listed in `AGENT_CORE_ADDONS` (or is a bridge whose
   dependencies are all listed). Nothing scans a directory. Installing or removing needs a restart.
2. **Declared.** An addon contributes what its `provides` declares, through the extension points
   below, and nothing else. A name that collides, a rule about a tool it does not own, or a file it
   did not declare stops startup naming the addon.
3. **Outside the kernel.** Addon Python never imports `agent_core.domain`, `.application` or
   `.ports` (one small exception, see "What addon code may import").
4. **Reviewed like code.** Addon code runs inside the Core process with the Core's privileges.
   Enabling an addon is the same decision as deploying code.

## Layout

```
addons/shop/
  addon.yaml            # the manifest (required)
  toolsets/cart.py      # provides.toolsets: [cart]
  toolsets/_common.py   # not declared: a helper the toolsets import (`from ._common import x`)
  wrappers/stamp.py     # provides.wrappers
  routers/keyword.py    # provides.routers: [keyword]
  fragments/note.md     # provides.fragments: fragments/
  profiles/clerk.yaml   # provides.profiles: profiles/
  policy/rules.yaml     # provides.policy
  migrations/0001_cart.sql
  hooks.py              # hooks.post_init: hooks:post_init
```

The directory name IS the addon name: lower case words joined by single underscores
(`decision_desk`; never a double underscore, it separates the setting variables).

## Manifest reference

```yaml
name: shop                       # = directory name
version: 1.2.0                   # a PEP 440 version; compared by number ("1.10" > "1.9")
core: ">=1.0,<2"                 # the Core versions this addon works with
depends: [http_tool_bridge]      # addons that must also be enabled; never enabled for you
auto_install: false              # true = bridge (needs at least one dependency)
provides: { ... }                # the extension points, below
settings: { ... }                # the schema of what an operator may set
hooks: { post_init: "hooks:post_init" }
migrations: migrations/
```

Unknown keys are errors everywhere except inside `settings`, which is the addon's own schema (a typo must not silently drop an extension point). A
version that is not a version, or a `core` range that is not a range, fails at manifest load.

### `provides`

| Key | Declares | Lives in | The rule |
|---|---|---|---|
| `toolsets: [name]` | a toolset a profile can list in `toolsets:` | `toolsets/<name>.py` with `build_toolset()` or `build_toolset(settings)` (the addon's own validated settings, D84) returning a `FunctionToolset` | tool and toolset names are global: a clash with Core or another addon stops startup. One addon may offer the same tool in several of its own toolsets (per-role sets, D87); a toolset or wrapper file may import helper modules beside it with a relative import |
| `audit.allow` / `audit.presence_only` | which arguments of YOUR tools the audit trail keeps (`tool: [argument]`) | `addon.yaml` only; a toolset module declaring `AUDIT_ALLOW` is refused | only your own tools; a tool with no decision records no arguments (and logs a warning) |
| `policy: path` | rules for your tools | one YAML file, the format of `Core/policy/*.yaml` | an `allow` may reach only YOUR tools (wildcards included); `deny` and `needs_approval` may narrow any tool. Needs a deployment that manages policy from files |
| `fragments: dir/` | prompt text a profile names in `include:` | `<name>.md` files; may read the addon's own settings, see "Fragments that read settings" | names are global |
| `profiles: dir/` | profile templates served beside Core's | `*.yaml` files | ids are global; a profile may `inherit` |
| `inserts: [{profile, slot, fragment}]` | where your fragment goes in someone else's profile | nothing (uses one of your `fragments`) | your own fragments only; the slot must exist. `profile: "*"` means every profile that declares the slot (D89) |
| `wrappers: [{tool, wrapper}]` | a wrapper around an existing tool | `wrappers/<wrapper>.py` with `wrap(original, arguments, ctx)` | a tool of Core or of an addon you depend on (directly or not); never your own |
| `routers: [name]` | a turn router a profile can name in `routing.router` (D95) | `routers/<name>.py` with `route(request)` or `route(request, settings)`, sync or async | names are global; consulted only when `AGENT_CORE_TURN_ROUTING` is on |
| `channels: [id]` | pull-mode channel ids a backend can start turns on | nothing (no code) | not an id Core or `AGENT_CORE_PULL_CHANNELS` owns |

A declared directory that is missing or empty is an error, not an empty contribution.

## Toolsets and audit

```python
# toolsets/cart.py
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset

def cart_add(sku: str, note: str) -> str:
    """Add an item to the cart."""
    return f"added {sku}"

def build_toolset() -> FunctionToolset[None]:
    return FunctionToolset([Tool(cart_add)])
```

```yaml
provides:
  toolsets: [cart]
  audit:
    allow: {cart_add: [sku]}            # recorded by value
    presence_only: {cart_add: [note]}   # recorded as "present, value not recorded"
```

A new tool is journaled (a crash mid-call answers `interrupted` instead of running it twice) unless
it declares itself replay-safe, see `agent_core.domain.tool_replay.safe_tool_metadata`.

## Policy

```yaml
# policy/rules.yaml
rules:
  - rule_id: shop-allow-cart-add     # global
    tool_pattern: cart_add
    effect: allow                    # allow | needs_approval | deny
    reason: Adding an item to the cart is reversible.
```

The rules join the set Core reconciles at startup, so removing the addon from the list and
restarting revokes them, exactly like deleting a rule from `Core/policy/`. Policy is decided on the
tool NAME before any wrapper runs.

## Profiles, inheritance, fragments and slots

A profile file works as any profile does, plus:

```yaml
id: shop_specialist
inherit: shop_assistant        # a Core profile or any addon template; at most 4 levels
max_iterations: 3              # scalars replace
toolsets: [refunds]            # lists append (parent first, no repeats); mappings merge
replace: [skill_namespaces]    # swap these top-level fields wholesale (never id, approval_rules)
slots: {before_tools: [house_style]}   # add default fragments to an inherited slot
```

`id`, `public_alias` and `peers.topology` are never inherited. A profile that wants other addons to
extend it declares a slot and marks where it goes, on a line of its own:

```yaml
slots: {before_tools: []}
persona: |
  You help with orders.

  {{slot:before_tools}}
```

Another addon fills it with `provides.inserts`; the text lands after the slot's defaults, in
activation order. A child that sets its own `persona` must keep the marker line of every inherited
slot that has content. The merged profile is validated like any profile, so inheritance grants no
authority. Details: OPERATIONS.md, "Profile inheritance and slots".

### Insert into every profile with a slot, and fragments that read settings (D89)

A generic addon cannot know which profiles will use it. `profile: "*"` in `provides.inserts` puts the
fragment into every profile that DECLARES the slot (a child inherits it through its parent, once). A
slot that no profile declares stops startup naming the addon, so a typo in the slot name is an error, not
a fragment that quietly applies to nothing. The profile opts in by declaring the slot and marking it.

```yaml
# addons/style/addon.yaml
provides:
  fragments: fragments/
  inserts: [{profile: "*", slot: house_style, fragment: style}]
settings:
  audience: {type: str, default: a shop manager}
  spoken: {type: bool, default: true}
```

```markdown
<!-- addons/style/fragments/style.md -->
Write for {{setting:audience}}.
{{if:spoken}}
When an answer is read aloud, use short sentences and no lists.
{{endif}}
```

A fragment of an addon may use the addon's own validated settings through two forms and nothing else:
`{{setting:name}}` (inline, replaced by the value) and a block whose `{{if:name}}` and `{{endif}}` markers
each sit alone on a line (kept when the setting is true: a non-empty string, a non-zero number, `true`).
An undeclared name, an unclosed or nested block and a stray `{{endif}}` stop startup naming the addon and
the file. Core's own fragments are never rendered.

`addons/plain_language` (D89) is the shipped example of both: a generic addon with one fragment, no code
and no vertical in it. A profile opts in by declaring the slot:

```yaml
slots: {plain_language: []}
persona: |
  You help with orders.

  {{slot:plain_language}}
```

and the operator edits the style in settings (`AGENT_CORE_ADDON__PLAIN_LANGUAGE__AUDIENCE`, `__LANGUAGE`,
`__AVOIDED_TERMS`, `__VOICE_STYLE`, `__VOICE_MARKER`). With `voice_style` on, a message that starts with
the marker is answered in short spoken sentences with no markdown.

## Wrappers (the `super()` analogue)

```python
# wrappers/stamp.py
async def wrap(original, arguments, ctx):
    result = await original()        # the tool, or the wrapper loaded before this one
    return f"{result} (checked)"
```

The tool keeps its name, schema and replay class. Policy, approvals and the audit row still apply
to the tool name and run BEFORE the wrapper, so a refused or pending call never reaches it. The
audit row is written once and lists the wrapping addons as `_wrapped_by`, a key only the runner
sets. `original()` takes no arguments (it runs with the arguments the policy decided on); `arguments`
is read-only. Wrappers on one tool nest in activation order, the last loaded outermost. An
exception from wrapper code reaches the model as a tool error with the text kept in the log. If a
tool itself takes an argument called `_wrapped_by` (reserved), the audit records it as
`_wrapped_by_received`, under the tool's own audit decision for that name (D81).

## Routers (before the model, D95)

A router reads what a person said before the model does and picks the way the turn goes. It is
plain data in and out: the addon imports nothing of Core.

```python
# routers/keyword.py
def route(request, settings):          # or route(request); `async def` works too
    # request = {"text", "profile_id", "channel", "tenant_id", "modality"}
    if "invoice" in request["text"].lower():
        return {"decision": "route", "profile_id": "billing",
                "label": "invoice", "confidence": 0.97, "calibrated": True, "model": "clf-1"}
    return {"decision": "pass", "label": "no_match"}
```

`decision` is `pass` (run as asked), `route` (run as `profile_id` instead) or `intent` (`intent`
and `arguments`: a typed intent; until intent executors exist it runs the normal path with the
reason recorded). Every other key is optional; an unknown key, a wrong type or a confidence outside
[0, 1] is a malformed answer and the turn runs as asked. A profile opts in:

```yaml
# profiles/front_desk.yaml
routing:
  router: keyword                # an enabled addon's provides.routers entry
  targets: [billing, returns]    # the ONLY profiles it may route to
```

A router is not a safety barrier. A route only changes WHICH profile runs the turn, and only to a
listed target that is loaded and that a channel may start; the routed turn runs that profile's
full policy, approvals and budgets. Only turns a person or a channel started are routed (never a
peer's ask or a subagent). A router slower than `AGENT_CORE_TURN_ROUTER_TIMEOUT_MS` (default 1500)
or one that raises runs the turn as asked. Each consultation is one `audit_turn_routes` row and a
`routed` progress event; the decision is a recorded workflow step, so a recovered turn never asks
the router again. With `AGENT_CORE_TURN_ROUTING` off (the default) nothing is consulted.

## Settings

`settings:` in the manifest is the schema; it is the only thing a vertical edits.

```yaml
settings:
  greeting: hello                       # a scalar: that type, that default
  limits:                               # a mapping without `type`: a group
    max_items: {type: int, default: 3, minimum: 1, maximum: 10}
  tags: {type: list, items: str, default: []}
  api_key: {type: str}                  # no default: the deployment must set it
```

Types: `str`, `int`, `float`, `bool`, `list`, `object`; `minimum`/`maximum` for numbers, `choices`
for `str`/`int`, `description` for the operator. Values are validated strictly at startup (a string
is not a number) and never echoed in an error. Precedence: manifest default < the file named by
`AGENT_CORE_ADDONS_CONFIG` < `AGENT_CORE_ADDON__<ADDON>__<KEY>` variables. A value for an inactive
addon or an undeclared key is an error. The validated values reach `post_init` as
`context.settings`.

## The HTTP tool bridge (`http_tool_bridge`, D83)

A shipped library addon: it provides no tool of its own and builds tools from a manifest YAML that a
consumer addon carries. It names no vertical. The consumer lists it under `depends` (it is never
enabled for you) and calls the builder from its toolset module:

```yaml
# addons/shop/addon.yaml
depends: [http_tool_bridge]
provides: {toolsets: [catalog], audit: {allow: {get_item: [item_id]}}}
```

```python
# addons/shop/toolsets/catalog.py
from pathlib import Path
from agent_core.adapters.driven.tools.http_bridge import build_bridge_toolset

def build_toolset(settings):    # the shop addon's validated settings (D84)
    return build_bridge_toolset(settings, manifest=Path(__file__).resolve().parent.parent / "tools.yaml")
```

```yaml
# addons/shop/addon.yaml (settings: any subset of the bridge keys below, plus the addon's own)
settings:
  base_url: {type: str, default: "http://localhost:8080"}
  service_name: {type: str, default: shop}
```

```yaml
# addons/shop/tools.yaml
api_prefix: /internal/v1               # optional
defaults: {context: [session, agent, tenant]}   # headers every tool sends unless it says otherwise
tools:
  - name: get_item
    description: One item by id.
    method: GET                        # GET | POST | PUT | PATCH | DELETE
    route: /items/{item_id}            # {x} = a `path` argument
    kind: read                         # read -> replay `safe`; write -> `interrupted` (override: replay:)
    arguments:                         # all the model may send; never a store, case or date
      item_id: {type: string, in: path, required: true}
      limit: {type: integer, in: query, default: 10, minimum: 1, maximum: 20}   # clamped
      kind: {type: string, in: query, enum: [a, b]}                             # else an error value
    context: [session, agent]          # session | agent | tenant | subject -> header names are settings
    response:
      pick: [id, name]                 # allowlist of keys (of an object, or of each row)
      rows_key: items                  # rows live under this key (omit: the body is the list)
      row_limit: 5                     # default: the `default_row_limit` setting
      wrap_as: reviews                 # return {reviews: <data>}
    warning: true                      # add the service's warning header (setting) as `warning`
  - name: reserve_item
    description: Reserve an item.
    method: POST
    route: /reservations
    kind: write
    idempotency: [item_id, slot]       # arguments keying the Idempotency-Key (session + parts)
    arguments:
      item_id: {type: string, in: body, required: true}
      slot: {type: string, in: body, required: true}
```

Rules: the manifest is validated when the consumer's toolset is built (a bad one stops startup naming
the file and field); an argument problem, an HTTP error, a timeout or a network failure is returned
to the model as `{"error": ..., "status"?: ...}`, never raised (an `application/problem+json` error body also adds a structured `problem` {type, title, status, detail} and puts `title: detail` in the message; any refusal whose body says why, a problem `detail` or `title`, a JSON `detail`/`message`/`error`/`title` text or a `text/plain` body, adds `reason`: that text on one line, at most 300 characters, D91); a reply with a warning and no
`wrap_as` gets a `warning` key (a list becomes `{"data": [...], "warning": ...}`). Settings
are the CONSUMER's own (D84): the addon loader hands the consumer's validated `settings:` to a
one-parameter `build_toolset(settings)`, and the bridge uses the keys it knows (`base_url`,
`timeout_seconds`, `connect_timeout_seconds`, `default_row_limit`, `session_header`, `agent_header`,
`tenant_header`, `subject_header`, `idempotency_header`, `warning_header`, `peer_session_prefix`
with its non-empty `peer_session_separator`, `service_name`; an empty header name turns that header
off) and ignores the rest. The bridge reads no environment, so two consumers can point at two
backends (`AGENT_CORE_ADDON__SHOP__BASE_URL`); `http_tool_bridge` itself has no settings. The arguments the policy and the audit row see are
the model's with every unsent argument filled: its `default`, or null when it has none (a `fixed`
one is never added; what the model sent stays as sent). That is what a Python tool's signature did,
so a tool moved onto the bridge keeps its audit trail; the request is the same either way (F1, D94).
Safety
rules of the builder: a path argument is one percent-encoded segment (an empty one, `.` or `..` is an
error value and no call is made); with `response.pick`, only the picked top-level keys of an object
or of each row reach the model (rows that are not objects are dropped; a reply that is not the
declared shape is an error value; a `rows_key` envelope keeps its other keys such as `total`);
integer bounds with a fraction round inward (`minimum: 1.5` -> 2). The library lives in
`agent_core.adapters.driven.tools.http_bridge` because addon code cannot import another addon (the
boundary scan forbids the import routes); an example manifest is
`Core/tests/fixtures/http_bridge/example_tools.yaml`.

### Tools from the backend's OpenAPI document (D85)

Instead of a hand-written `tools.yaml`, a consumer can let the bridge read the backend's own OpenAPI
3.x document and build the SAME tools from it. Only operations marked `x-agent-tool: true` become
tools; the model sees nothing else.

```python
def build_toolset(settings):
    here = Path(__file__).resolve().parent.parent
    return build_bridge_toolset(
        settings,
        openapi=f"{settings['base_url']}/openapi.json",   # an http(s) URL (fetched ONCE, now), a file path or a dict
        overlay=here / "overlay.yaml",                    # optional manual manifest, see below
        expect=["get_item", "reserve_item"],              # optional: these tools must exist
    )
```

Exactly one of `manifest=` (the YAML above, still the fallback for a backend without OpenAPI) and
`openapi=` is passed. The marks, all on the operation:

| Mark | Meaning | Default |
|---|---|---|
| `x-agent-tool: true` | the operation is a tool (a non-boolean is an error) | not a tool |
| `x-agent-effect: read\|write` | replay `safe` / `interrupted` | GET -> read, other methods -> write |
| `x-agent-name` | tool name (`^[a-z][a-z0-9_]*$`) | `operationId` |
| `x-agent-description` | text the model reads | `summary`, then `description` |
| `x-agent-fields: [..]` | the model-visible projection (`response.pick`) | the whole reply |
| `x-agent-rows-key`, `x-agent-row-limit` | rows under this key; rows returned | body is the list; `default_row_limit` |
| `x-agent-replay: safe\|interrupted` | replay after a crash | from the effect |
| `x-agent-context: [session, agent, tenant, subject]` | identities sent as headers | `session, agent, tenant` |
| `x-agent-warning: true` | surface the warning header | off |
| `x-agent-idempotency: [arg, ..]` or `true` (all arguments) | what keys the `Idempotency-Key` of a write | none |

Arguments come from `parameters` (path-item, then operation level) and from a flat
`application/json` object `requestBody` (each property is a body argument). Types, `enum`,
`minimum`/`maximum`, `default` and `required` are read from the schema; local `$ref`s are followed,
`type: [X, "null"]` and `anyOf: [X, null]` are an optional X. Header parameters are skipped (the bridge
sends the identity headers, never the model). Everything else is refused at startup, naming the
operation (`GET /things (the_op)`): remote `$ref`s, cookie parameters, `oneOf`/`anyOf` with several
real alternatives, exclusive bounds, a body that is not a flat JSON object, a schema without a type,
duplicate tool names, no marked operation at all, and `expect=` names the document does not provide.
A backend that drops or breaks a marked operation therefore stops the deployment instead of the
conversation.

The document is fetched ONCE per process and URL (several toolsets of one consumer share the
fetch; a failed fetch is not remembered and stops the startup naming the URL, so the backend must
answer when Core starts). A sync or an async-only `httpx` transport is honoured for that fetch.

The overlay is a manual manifest (`tools:`, optional `defaults:` and `api_prefix:`; a file or a dict).
An entry named like a generated tool PATCHES it (`arguments` and `response` merge key by key, other
fields replace); `operation: "GET /items/{item_id}"` pins the operation behind the tool and fails
the startup when the document moved it. An entry with no twin must be a complete manual tool (its
route gets the overlay's `api_prefix`); a partial entry that matches nothing is an error listing what
the document does mark.

Three overlay features exist for a backend whose operations expose more than the model should see
(D87). In a patch, `arguments: {store_id: null}` REMOVES an argument (it is not in the schema and is
never sent; removing one the operation does not have is an error). `fixed: 10` on an argument (also in
a manual `tools.yaml`) sends that value on every call and keeps the argument out of the model's schema;
the model cannot override it, and a `fixed` value replaces the document's bounds, default and required
flag for that argument. A complete manual tool may carry `operation: "GET /internal/v1/issues"`: the
document must declare that operation and it must be the tool's own method and route, so a second tool
over one operation (another name, a fixed filter) still stops the startup when the backend moves it.

A large consumer worked end to end is `addons/glazed_tools` (D87): thirty tools from one backend's
OpenAPI document, an overlay of bounds, hidden arguments and pins, eight per-role toolsets sharing one
helper module, and a few tools whose answer is shaped in code (forecasts, manager views).

### The glazed audit and policy (`glazed_tools`, D88)

`addons/glazed_tools/addon.yaml` declares which arguments of its thirty tools the audit keeps and
`policy/rules.yaml` the 33 rules that decide who may call them (read rows for `peer` and
`glazed-service`, the manager's own two writes and `get_briefing`, the two peer-turn denies and a
`glazed_*` catch-all deny). Enabling the addon needs a deployment with a policy directory
(`AGENT_CORE_POLICY_DIR`): without one startup stops naming `glazed_tools`.

The one glazed rule an addon cannot carry is the grant of Core's `ask_peer` to the orchestrator. The
glazed deployment adds it to its own policy directory:

```yaml
rules:
  - rule_id: glazed-orchestrator-asks-specialists
    tool_pattern: ask_peer
    effect: allow
    subject_roles: [glazed-manager]
    channels: [glazed]
    reason: >-
      The orchestrator delegates to its specialists. The two-sided allowlist and the hop
      limit still decide which peer.
```

### The glazed agents and channel (`glazed_vertical`, D90)

`addons/glazed_vertical` is the worked example of a vertical built only from addons: it ships no code,
the eight copilot roles as profiles and the pull-mode channel `glazed`. It depends on `glazed_tools`
(their toolsets and policy) and `plain_language` (the orchestrator's manager-communication text).

- `profiles/glazed_base.yaml` holds what the eight share (model, compaction, media, the specialists'
  way back to the orchestrator, switched off) and is harmless on its own: no tools, no peers, one
  iteration. Each role `inherit`s it and sets its persona, toolset and budget; a specialist adds
  `peers: {enabled: true}`, the orchestrator `replace: [peers]` with its own star.
- The orchestrator declares the slot `plain_language` where the vertical branch had "How to talk to the
  manager"; the generic text lands there and the glazed-only words (the Spanish examples, rounding in
  voice, Rule 3 in plain words) follow it in the persona.
- An addon cannot set another addon's settings, so the glazed values for `plain_language` (Spanish, the
  Spanish jargon list, `[canal: voz]`) are in `addons/glazed_vertical/addons_config.yaml`; the deployment
  points `AGENT_CORE_ADDONS_CONFIG` at it.

```
AGENT_CORE_ADDONS=http_tool_bridge,glazed_tools,plain_language,glazed_vertical
AGENT_CORE_ADDONS_CONFIG=<addons dir>/glazed_vertical/addons_config.yaml
```

### Migrating from `AGENT_CORE_ADDON__HTTP_TOOL_BRIDGE__*` (D86)

Before D84 the bridge keys were set on the bridge itself. They now belong to the consumer addon that
uses it: `AGENT_CORE_ADDON__HTTP_TOOL_BRIDGE__BASE_URL` becomes `AGENT_CORE_ADDON__<CONSUMER>__BASE_URL`
(same for every key in the list above, and in the `AGENT_CORE_ADDONS_CONFIG` file under the consumer's
name). The old names are refused at startup, and the error says so: `http_tool_bridge has no settings of
its own, set the key on the addon that depends on it (AGENT_CORE_ADDON__SHOP__BASE_URL)`. The consumer
must declare the keys it wants an operator to edit in its own `settings:`.

### Vertical internal API conventions

What a vertical backend does so the bridge can drive it without per-vertical code. Every item is a
convention the bridge defaults to, and each header name is a setting.

- Path prefix `/internal/v1`: the agent-facing API, separate from the public one, not exposed
  outside the network. The OpenAPI paths carry the prefix (the bridge calls them as written).
- Context headers `X-Agent-Session`, `X-Agent-Id`, `X-Agent-Tenant` (and `subject_header`, off by
  default), sent per `x-agent-context`. The backend trusts them only from the agent; a vertical with
  existing names (`X-Glazed-*`) sets `session_header` and friends in its addon settings.
- `Idempotency-Key` on writes: a deterministic key (session + the keyed arguments) so a retried call
  does not create a second row; the backend stores the first result under the key.
- Errors as `application/problem+json` (RFC 9457): `type`, `title`, `status`, `detail`. The bridge
  returns `{error, status, problem, reason}` to the model, so `detail` is what the model can say.
- `X-Agent-Warning` response header (`warning_header`) for a degraded answer ("memory unavailable");
  a tool with `x-agent-warning: true` shows it to the model as `warning`.
- Row limits: a list answers `{items: [...], total: n}` (or a bare list), the backend caps it, and
  the tool's `x-agent-rows-key` / `x-agent-row-limit` bound what the model reads.
- Marks, not magic: a route becomes a tool only when its author writes `x-agent-tool` on it. With
  FastAPI:

```python
@router.get(
    "/internal/v1/items/{item_id}",
    operation_id="get_item",
    summary="One item by id.",
    openapi_extra={"x-agent-tool": True, "x-agent-fields": ["id", "name", "price"]},
)
def get_item(item_id: str) -> Item: ...

@router.post(
    "/internal/v1/reservations",
    operation_id="reserve_item",
    summary="Reserve an item for the caller.",
    openapi_extra={"x-agent-tool": True, "x-agent-idempotency": ["item_id", "slot"]},
)
def reserve_item(body: Reservation, idempotency_key: str = Header(alias="Idempotency-Key")) -> Created: ...
```

## Migrations and `post_init`

```yaml
version: 1.1.0
migrations: migrations/        # 0001_cart.sql, 0002_label.sql
hooks: { post_init: "hooks:post_init" }   # addons/shop/hooks.py
```

```python
# hooks.py  (synchronous: an async hook is refused when the addon is loaded)
def post_init(context):
    context.connection.execute("INSERT INTO shop_labels VALUES (%s)", (context.version,))
```

On every start, after Core's schema, each enabled addon is brought up to its manifest, dependencies
first: every `migrations/*.sql` file not yet recorded runs in file-name order, one transaction per
file; then, only if the addon is new or its version changed, `post_init` runs and the version is
recorded in one transaction. `context` has `name`, `version`, `previous_version` (`None` on first
install), `settings` and `connection` (inside that transaction: do not commit or close it). A hook
must be safe to repeat, because a failure re-runs it. Never edit a migration that already ran:
add a new file.

## Lifecycle

1. Read `AGENT_CORE_ADDONS`; load each `addon.yaml` (no addon code is imported yet).
2. Resolve: dependency order, cycle / missing dependency / incompatible Core version fail.
3. Activate bridges whose dependencies are all enabled.
4. Validate settings.
5. Register: import the declared toolset and wrapper modules; seam by seam, validate and merge.
6. Install: migrations, then `post_init`, then record versions (one process at a time).
7. Reconcile policy, build the container.

Everything above happens at startup; an error never appears mid-turn. With no addon enabled each
step is a no-op and Core starts exactly as it did before.

## Errors

All are raised at startup and name the addon and the field: `ManifestError` (invalid manifest),
`AddonResolutionError` (graph), `AddonSettingsError`, `AddonRegistrationError` (an extension point
refused or colliding), `AddonInstallError` (a migration or hook failed, a downgrade, an unreachable
database during the install pass). An install error names the step that failed (the lock, the
recorded versions, one migration file, `post_init`, recording the version); field `database` means
the connection itself was lost or refused, never a SQL error or a failing hook. A hook must be a
plain `def`: an `async def`, a callable object with an async `__call__`, or any hook that returns a
coroutine is refused. `python -m agent_core addons list` shows what is installed and
what the next start does.

## What addon code may import

The contract test `tests/contract/test_addon_boundary.py` scans every `.py` under `addons/`.
Forbidden: `agent_core.domain`, `agent_core.application`, `agent_core.ports` and anything under
them, and the routes around an import (`__import__`, `importlib`, `import_module`, `exec`, `eval`,
`sys.modules`, `builtins`), including reaching a protected package through an alias or attribute
(`import agent_core as c; c.domain`). Methods that only share a name (`obj.exec()`) are fine. Allowed: the standard library, `pydantic_ai`, other third-party
packages, `agent_core.adapters.*` (for example `AddonInstallContext`), and the one allowed
inner-layer surface, `agent_core.domain.tool_replay` (pure helpers to mark a tool replay-safe).
Widening that list is a reviewed change to the test helper and to this section.

## Limitations and open defaults

- The boundary check is static and best effort; it is not a sandbox. Addon code can still do
  anything Python can. Review addons like code.
- Install and removal need a restart. Removing an addon keeps its rows and tables (a returning
  addon continues); uninstalling is a documented manual step. A downgrade refuses startup.
- Migrations are a flat ordered directory, not `migrations/<version>/`; they are not checksummed,
  and a failing file rolls back alone (the earlier ones stay). One `post_init`, synchronous.
- Wrapper `arguments` are read-only; an addon cannot wrap its own tool; the last loaded wrapper is
  outermost; the `{{slot:x}}` marker has no escape.
- A router answers within one call (no streaming, no retries of its own); `intent` decisions run the
  normal path until intent executors exist; one router per profile.
- An addon policy file needs a policy directory; a channel id clashing with the operator's
  `AGENT_CORE_PULL_CHANNELS` is a collision.
- Each of these is a reversible default recorded in DECISIONS.md.
