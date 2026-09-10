"""Driven adapter: ToolPolicy over Postgres.

Phase:   F1 / D2 (tenant dimension)
Tasks:   docs/TASKS.md#t-f1-15
Implements: ports/tool_policy.py

SILENT-BUG AREA. A hole here never fails a test.

TABLE
    policy_rules (rule_id pk, tenant_id null, tool_pattern, effect,
                  subject_roles text[], channels text[], reason)

    Add `tenant_id` NOW even though it is unused until D2. NULL means "all tenants".
    Altering a table that is already the authority on permissions is a migration nobody
    enjoys.

decide() PSEUDO-CODE
    1. Load the caller's applicable rules ONCE PER TURN and cache. Never query per tool
       name - this is on the hot path of every turn.
    2. Match, then reduce by EFFECT_PRECEDENCE: DENY > NEEDS_APPROVAL > ALLOW.
    3. No match -> DENY.

TWO DEFAULTS THAT MUST NOT DRIFT
    - No matching rule -> DENY. An unknown tool is a typo or an unregistered addition;
      both deserve refusal. ALLOW-by-default means every newly imported tool is silently
      world-usable and nothing warns you.
    - Store unreachable -> DENY (fail closed) and surface the error. An agent running
      unrestricted because Postgres blipped is worse than an agent that stops.
"""
