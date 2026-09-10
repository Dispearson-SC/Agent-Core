"""Driven adapter: ContextEngine - the compaction ladder.

Phase:   F5
Tasks:   docs/TASKS.md#t-f5-04
Implements: ports/context_engine.py

SILENT-BUG AREA. Wrong here = a bigger bill, never a red test.

BUDGET: ~750 LINES. THAT NUMBER IS THE DISCIPLINE, NOT AN ASPIRATION.
    Hermes spent 11,003 lines across 8 files on this subsystem - context_compressor.py
    alone is 4,918. That gap is not bad engineering: it is the cost of chasing semantic
    deduplication, provider-native compaction, images inside history, commit fences and
    repair of broken histories.

    Treat the L1-L4 ladder as the COMPLETE day-1 scope. Do not add a rung until the bill
    or the answer quality demands it with data.

THE LADDER (see domain/compaction.py for the rung semantics)
    L1 prune tool outputs to stubs                free
    L2 sliding window, protected head             free
    L3 summarise the middle with a cheap model    one call
    L4 fold the previous summary into a new one   one call

    Stop as soon as target_fraction is met. Running L3 when L1 already freed enough is
    paying for a model call that bought nothing.

THE TWO INVARIANTS, BOTH TESTED PER RUNG
    1. Tool call / tool return pairing survives every cut. Breaking it makes the provider
       reject the conversation with a 400, far from the compaction that caused it.
    2. `should_compress` handles `window_used is None` by falling back to the local token
       estimate. Otherwise a provider that stays silent means the agent never compacts and
       dies of overflow - in production, never in tests.

CONCURRENCY COMES FROM DBOS
    This engine does not need locks, snapshots or commit fences. `compress` returns a
    result and does not mutate; the caller runs inside a @DBOS.step() which supplies the
    durable lock and safe retry. Hermes wrote hundreds of lines for this. Do not.
"""
