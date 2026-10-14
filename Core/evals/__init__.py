"""The eval harness (C32, D67): scripted multi-agent scenarios run against a real model.

NOT part of the unit suite. `pytest` collects `tests/` only; this package is run on purpose, with
`make eval` (see the root Makefile) and its `AGENT_CORE_EVAL*` environment variables, because a run
calls a real provider, takes minutes and costs money. The machinery itself is tested, against the
scripted fake model, by `tests/unit/test_evals.py`.

    harness.py    scenario / trial / report types, `run_eval`, the JSON and Markdown report
    sources.py    where the models come from: a real provider, or the scripted fake
    scenarios.py  the scenarios (each: a multi-agent flow and the checks its outcome must pass)
    __main__.py   the opt-in switches and the entry point (`python -m evals`)
    routing.py    the routing benchmark: labelled question sets, routing off vs on, the metrics
    routing_cli.py  its entry point (`python -m evals.routing_cli`; the offline run needs no opt-in)

Dev tooling, so it may import the test fakes (the scripted model, the in-memory ports) that the
unit suite already trusts. Nothing under `src/` imports it.
"""
