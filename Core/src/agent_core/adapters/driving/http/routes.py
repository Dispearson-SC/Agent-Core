"""Driving adapter: FastAPI routes.

Phase:   F0 (one route) / F3 (decision route) / F7 (upload route)
Tasks:   docs/TASKS.md#t-f0-03
Implements: nothing - it CALLS use cases

ROUTES
    POST /turns                  start a turn; returns turn_id immediately, does NOT block
    GET  /turns/{turn_id}        poll status
    POST /decisions/{corr_id}    a human approves or refuses            (F3)
    POST /evidence/{corr_id}     a human uploads a requested file       (F7)

THE ONE RULE THAT SHAPES ALL OF THEM
    A route NEVER waits for a turn. It starts the workflow and returns. The durable wait
    is DBOS.recv() inside the workflow.

    A route that blocks on an approval holds a connection for three days, dies on the next
    deploy, and takes the turn with it.

PSEUDO-CODE - POST /turns
    1. Authenticate -> CallerIdentity. This is the input to every policy decision, so
       getting identity wrong here silently mis-authorises everything downstream.
    2. Build TurnRequest.
    3. Start the DBOS workflow (async handle).
    4. Return 202 with turn_id.

PSEUDO-CODE - POST /decisions/{corr_id}
    1. DecideApproval.execute(...) -> (turn_id, tool_call_id)
    2. DBOS.send(turn_id, payload) to wake the waiting workflow.
    3. Unknown or expired handle -> 404. NEVER guess: routing a stray reply into the
       wrong turn approves an action nobody approved, and it looks like success in the log.
"""
