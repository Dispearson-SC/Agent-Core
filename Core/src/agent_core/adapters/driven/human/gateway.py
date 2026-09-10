"""Driven adapter: HumanGateway - publish asks, correlate replies.

Phase:   F3 (approvals) / F7 (evidence, unchanged)
Tasks:   docs/TASKS.md#t-f3-04
Implements: ports/human_gateway.py

NO WAITING HAPPENS HERE
    This adapter publishes and correlates. The durable wait is DBOS.recv() in the workflow.
    A sleep, a poll loop or a thread join in this file is a bug: it will silently lose the
    turn on the next deploy.

CORRELATION TABLE
    human_requests (correlation_id pk, turn_id, tool_call_id, kind, expires_at, answered_at)

    `correlation_id` is handed to a human over a channel. Make it unguessable - anyone who
    can guess one can approve an action. Treat it like a bearer token, because it is one.

IDEMPOTENCY
    publish() must be idempotent per (turn_id, tool_call_id). A DBOS step retries after a
    crash, and re-publishing asks a real person the same question twice - which is how you
    end up with two conflicting approvals for one action.

REDACTION
    Never put raw tool arguments in a channel message without redacting. Arguments carry
    credentials and personal data, and the channel is almost always less trusted than the
    database.
"""
