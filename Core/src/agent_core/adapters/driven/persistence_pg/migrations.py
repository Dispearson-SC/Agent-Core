"""Schema migrations.

Phase:   F1
Tasks:   docs/TASKS.md#t-f1-16

TWO LOGICAL DATABASES ON ONE INSTANCE (day 1)
    app   - turns, messages, checkpoints, policy_rules, audit_*
    dbos  - DBOS's own workflow and step state; it owns this, we never touch it

Day 2 adds a THIRD database on the SAME instance for LiteLLM. A second Postgres instance
is never required by the design - splitting instances is an operations decision about
blast radius and backups, worth taking when an incident justifies it.

RULE
    Migrations are forward-only and never destructive on audit tables. An audit table that
    can be dropped by a migration is not an audit table.
"""
