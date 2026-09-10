"""In-memory fakes — one per port.

Deliberately no count in this docstring. The port count has already drifted once (eleven
to fifteen) and left four stale strings behind it across config, docs and this file.
docs/ARCHITECTURE.md section 3 is the single source of truth; everywhere else states the
invariant instead: only two ports change when a vertical is added.


Hermes shipped ~3,821 test files and NOT ONE reusable fake model provider - every test
hand-rolled a MagicMock over the client. The Hermes-Core extraction had to build one
before anything could be tested at all.

We build the fakes FIRST, in F0, for exactly that reason. A fake that records calls is
worth more than a mock that asserts them: tests read better and break less.

TODO(F0): implement FakeAgentRunner and FakeToolProvider - enough for the F0 skeleton.
TODO(F1): the rest.
"""

# TODO(F0): class FakeAgentRunner       - returns a scripted TurnOutcome; records inputs
# TODO(F0): class FakeToolProvider      - a fixed toolset
# TODO(F1): class FakeToolPolicy        - rules injected in the constructor
# TODO(F1): class FakeConversationStore - a list
# TODO(F1): class FakeAuditSink         - a list; assert ORDER, not just content
# TODO(F3): class FakeHumanGateway      - captures published asks, replays answers
# TODO(F5): class FakeContextEngine     - scripted should_compress/compress
# TODO(F6): class FakeSkillRegistry     - dict of SkillMeta
# TODO(F7): class FakeMediaStore        - dict keyed by sha256
