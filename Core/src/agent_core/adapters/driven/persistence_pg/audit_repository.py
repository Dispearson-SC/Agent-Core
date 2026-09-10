"""Driven adapter: AuditSink over Postgres.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-14
Implements: ports/audit_sink.py

NON-NEGOTIABLE #6 - THE WHOLE REASON THIS FILE IS SEPARATE
    Writes go through a SEPARATE connection pool, outside the domain transaction.

    Sharing the pool means a failed turn rolls back its own evidence, and the one turn you
    most need to explain is the one that left no trace. This is not theoretical: it is the
    default behaviour if you wire it the obvious way.

TABLES (all append-only, no UPDATE, no DELETE)
    audit_tool_calls        turn_id, caller, tool, arguments jsonb, effect, rule_id, at
    audit_human_decisions   turn_id, tool_call_id, subject_id, approved, note, at
    audit_media             turn_id, media_id, sha256, direction, at
    audit_turn_costs        turn_id, input_tokens, output_tokens, cached, cost_usd, at

REDACTION
    Arguments carry credentials, tokens and personal data. Use a per-tool ALLOWLIST of
    fields to store, never a denylist of key names - a denylist misses the field somebody
    adds next month, and misses it silently.

audit_turn_costs IS THE COMPACTION DASHBOARD
    Cost per turn over a long conversation is the ONLY place a bad compaction strategy is
    visible. Rising cost after enabling compaction means the trigger is too low and the
    prompt cache is being destroyed. No test reports this.
"""
