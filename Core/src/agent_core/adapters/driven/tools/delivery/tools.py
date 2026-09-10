"""Vertical: delivery optimizer tools.

Phase:   F4 - FIRST VERTICAL, and the contract test
Tasks:   docs/TASKS.md#t-f4-01
Per-vertical: YES

WHY DELIVERY IS FIRST AND NOT FRAUD
    Short turns, bounded domain, simple approvals. It exercises the entire core without
    regulatory weight, and its domain complexity will not mask architectural errors.

    F4 exists to prove the port contract holds. It should not also be the hardest domain
    available. docs/DECISIONS.md#d11.

TOOLS
    routing_estimate(order_id)                read-only
    pricing_quote(order_id)                   read-only
    pricing_apply(order_id, new_price)        MUTATING - approval above 15% change
    orders_lookup(order_id)                   read-only
    request_evidence(kind, reason)            deferred, external      (F7)

THE ACCEPTANCE CRITERION FOR THIS WHOLE PHASE
    The vertical works end to end AND the diff touches no file under domain/, application/
    or ports/. If it does, the port cut is wrong - stop and revisit it before continuing.
    CLAUDE.md.

    tests/unit/test_contract.py enforces this. Do not weaken that test to make a change
    pass; that is the one shortcut that quietly ends the architecture.
"""
