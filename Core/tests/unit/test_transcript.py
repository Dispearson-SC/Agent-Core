"""Transcript visibility invariants - the grid decides, never a call site.

Phase:   F10 - Transcript and audit API
Tasks:   docs/TASKS.md#t-f10-01, docs/TASKS.md#t-f10-07

Non-negotiable #11: transcripts store everything and filter on read. The visibility grid is
the single place that filtering is expressed, so a kind the grid does not mention is a kind
nobody decided about.

WHY THIS FILE WAS REWRITTEN
    The first version of the anchor read every value of `VISIBLE_TO` and asked whether each
    `EntryKind` appeared in at least one of them. `VISIBLE_TO[Audience.ADMIN]` was
    `frozenset(EntryKind)` - derived from the enum - so every kind appeared in it the
    instant it was declared. The assertion could not fail. It was not a guard; it was a
    restatement of the enum.

    The grid is now written cell by cell and `VISIBLE_TO` is derived FROM it. A missing
    kind, a missing audience, or a half-answered row fails here.

`t-f10-08` (the user projection never leaks a tool name) and `t-f10-09` (a query cannot
cross a tenant) own their own tests and need the projection that does not exist yet. What
this file pins is the grid those tests will read.
"""

from agent_core.domain.transcript import VISIBILITY, VISIBLE_TO, Audience, EntryKind


def test_visible_to_covers_every_entry_kind() -> None:
    """The anchor assertion: no EntryKind is missing a visibility decision.

    Asked of `VISIBILITY`, not of the derived `VISIBLE_TO`. Asking the derived view is what
    made the previous version of this test unfalsifiable - a kind auto-admitted to ADMIN
    answers "yes, somebody can see it" without anybody having decided.
    """
    undecided = [kind for kind in EntryKind if kind not in VISIBILITY]

    assert undecided == [], (
        f"EntryKind members with no row in VISIBILITY: {undecided}. "
        "Adding a kind means deciding, for every Audience, whether it may be seen."
    )


def test_every_row_answers_every_audience() -> None:
    """A row that mentions one audience and forgets the other is half a decision.

    This is the other axis of the same guard: adding an `Audience` must fail here too,
    because a new audience with no answer in any row is exactly as undecided as a new kind.
    """
    incomplete = {
        kind: sorted(set(Audience) - set(row))
        for kind, row in VISIBILITY.items()
        if set(row) != set(Audience)
    }

    assert incomplete == {}, f"rows that do not answer every Audience: {incomplete}"


def test_admin_visibility_is_declared_and_not_the_whole_enum() -> None:
    """The regression lock for the defect this file was rewritten for.

    If ADMIN ever equals every `EntryKind` again, the coverage question above becomes
    satisfiable by the enum alone and stops being a question. Today ADMIN legitimately does
    not see `PENDING_PLACEHOLDER`: the placeholder is the substitute a USER gets INSTEAD of
    `PENDING_REQUEST`, and an operator reads the real request.
    """
    assert VISIBLE_TO[Audience.ADMIN] != frozenset(EntryKind), (
        "ADMIN is visible to every kind again. Derive it from the enum and the coverage "
        "test above can never fail."
    )
    assert EntryKind.PENDING_PLACEHOLDER not in VISIBLE_TO[Audience.ADMIN]
    assert EntryKind.PENDING_REQUEST in VISIBLE_TO[Audience.ADMIN]


def test_visible_to_is_exactly_the_declared_grid() -> None:
    """`VISIBLE_TO[audience]` stays the lookup the reader uses; the grid is where it comes
    from. If the two ever disagree, one of them is a second write path."""
    assert set(VISIBLE_TO) == set(Audience)

    # `.get(..., False)` deliberately, not `row[audience]`: an unanswered cell is the
    # business of `test_every_row_answers_every_audience`, which names it. Indexing here
    # would pre-empt that with a KeyError and hide the readable failure behind a traceback.
    for audience in Audience:
        declared = frozenset(
            kind for kind, row in VISIBILITY.items() if row.get(audience, False)
        )
        assert VISIBLE_TO[audience] == declared, (
            f"VISIBLE_TO[{audience}] disagrees with VISIBILITY"
        )


def test_user_never_learns_which_tool_is_pending() -> None:
    """Non-negotiable #11, first half.

    A USER must not learn WHICH tool is pending. That is not one kind: the tool name leaks
    through the call, through its result, and through the pending request itself.
    """
    naming_a_tool = {
        EntryKind.TOOL_CALL,
        EntryKind.TOOL_RESULT,
        EntryKind.PENDING_REQUEST,
    }

    leaked = sorted(naming_a_tool & VISIBLE_TO[Audience.USER])

    assert leaked == [], f"USER can see kinds that name a tool: {leaked}"


def test_user_still_learns_that_something_is_pending() -> None:
    """Non-negotiable #11, second half - and the half that is easy to lose.

    Hiding suspension entirely makes the conversation look broken: the agent waits three
    days for an approval and the user sees silence. `PENDING_PLACEHOLDER` says THAT
    something is pending without saying what.
    """
    assert EntryKind.PENDING_PLACEHOLDER in VISIBLE_TO[Audience.USER], (
        "USER cannot see that anything is pending. A suspended conversation is then "
        "indistinguishable from a broken one."
    )


def test_user_sees_the_conversation_itself() -> None:
    """The floor under the redaction: filtering on read must not filter away the messages.

    A grid that hid these would pass every assertion above and still return an empty
    conversation to the person having it.
    """
    for kind in (EntryKind.USER_MESSAGE, EntryKind.AGENT_MESSAGE):
        assert kind in VISIBLE_TO[Audience.USER], f"USER cannot see {kind}"
