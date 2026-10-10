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
    audit_tool_calls          turn_id, caller, tenant_id, tool, arguments jsonb, effect,
                              rule_id, reason, at
    audit_human_decisions     turn_id, tool_call_id, subject_id, approved, note, at
    audit_rejected_decisions  turn_id, tool_call_id, subject_id, reason, at
    audit_media               turn_id, media_id, sha256, direction, at
    audit_turn_costs          turn_id, input_tokens, output_tokens, cached, cost_usd, at

    `audit_rejected_decisions` IS A TABLE OF ITS OWN, and it is the storage half of the
    argument in `ports/audit_sink.py#record_rejected_decision`. A refused attempt has no
    verdict, so it has no `approved` column to fill; writing it into
    `audit_human_decisions` would need one, and whichever value went there would assert a
    decision the human never made. Two members over one table would put that lie back.

    IT HAS NO MIGRATION YET. `migrations.py` stops at `0008_audit_turn_costs`, so this
    one INSERT raises `UndefinedTable` against a real database until a forward-only
    `0009_audit_rejected_decisions` is added there. Nothing calls this member yet either
    (`DecideApproval` still raises `FourEyesError` without writing), so the gap is
    reachable only by wiring the use case - and both halves belong to the same anchor.
    Stated here rather than left to be discovered, because a sink member that cannot
    write is the exact silent shape non-negotiable #6 exists to prevent.

REDACTION
    Arguments carry credentials, tokens and personal data. Use a per-tool ALLOWLIST of
    fields to store, never a denylist of key names - a denylist misses the field somebody
    adds next month, and misses it silently.

    AN EMPTY ALLOWLIST IS NOT A SAFE DEFAULT, IT IS AN ABSENT DECISION (t-f11-35). This
    map shipped as `{}` for eleven waves, so `_redact` kept nothing and every call in the
    trail rendered "arguments: none recorded". Deny-by-default is right for a PERMISSION:
    the cost of not deciding is that something cannot happen. For a RECORD the cost runs
    the other way - the trail is blank exactly where an incident needs it, and "we do not
    know what this agent did" is the one answer this table must never give.

    So each field of each shipped tool is opted in DELIBERATELY, and the decision per
    field is between two dispositions, not between on and off:

      VALUE    (`ARGUMENT_ALLOWLIST`)     the operator needs to read what was passed
      PRESENCE (`ARGUMENT_PRESENCE_ONLY`) the operator needs to know it was passed

    A delivery address is personal data; a delivery code is an identifier. The address
    answers nothing the row is read for and outlives the reason it was collected, so if it
    were ever an argument it would be PRESENCE. The code names the thing the call was
    about and is the only way to line the row up against the order, so it is VALUE. That
    distinction is the whole design, and it is stated next to each entry below.

audit_turn_costs IS THE COMPACTION DASHBOARD
    Cost per turn over a long conversation is the ONLY place a bad compaction strategy is
    visible. Rising cost after enabling compaction means the trigger is too low and the
    prompt cache is being destroyed. No test reports this.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from decimal import Decimal
from typing import Any

from agent_core.domain.media import MediaRef
from agent_core.domain.policy import PolicyDecision
from agent_core.domain.turn import CallerIdentity, ToolCallId, TurnId, Usage

# A source of connections that belong to THIS sink. `psycopg_pool.ConnectionPool.connection`
# satisfies it directly, which is how composition hands over the separate audit pool; a
# plain `lambda: psycopg.connect(conninfo, autocommit=True)` satisfies it too.
#
# The sink takes a factory rather than a connection on purpose: a method that accepts a
# connection can be handed the domain transaction's one, and then non-negotiable #6 is a
# convention instead of a structure.
ConnectionFactory = Callable[[], AbstractContextManager[Any]]

# What a PRESENCE-only field is stored as. A fixed sentence, never the value, and never
# `True` - the console prints `name = <value>` straight from this jsonb (cli/console.py
# `_render_arguments`, which deliberately does not redact a second time), and an operator
# reading `note = True` would have to guess what that meant.
PRESENCE_RECORDED = "<present, value not recorded>"

# Per-tool ALLOWLIST of argument fields stored BY VALUE, never a denylist of key names.
# Looked up per tool, so a tool nobody has opted in stores no arguments at all - see
# `_redact`, which says that out loud rather than dropping them in silence.
#
# A vertical supplies its own map through the constructor, and the shipped verticals are
# listed here; either way no file under domain/, application/ or ports/ changes to add one.
# The entries below cover exactly the tools `composition.TOOL_PACKAGES` serves - the
# delivery package and the fraud package - and each says why the operator needs the value.
ARGUMENT_ALLOWLIST: Mapping[str, frozenset[str]] = {
    # --- delivery package (adapters/driven/tools/delivery/tools.py) ---
    #
    # `order_id` is an IDENTIFIER, not personal data. It names the order the call was
    # about and it is the only way to line an audit row up against the order it changed;
    # without it the row says an agent priced something and cannot say what. It carries no
    # customer detail of its own - the address, the name and the phone live in the orders
    # system behind their own retention rules, and none of them is ever an argument here.
    "orders_lookup": frozenset({"order_id"}),
    "routing_estimate": frozenset({"order_id"}),
    "pricing_quote": frozenset({"order_id"}),
    # `new_price` is the NUMBER THE RULE WAS EVALUATED AGAINST. `delivery_optimizer.yaml`
    # gates this tool on `abs(pct_change) > 15`, so the amount is the entire subject of
    # the approval decision on this very row, and it is the one field that cannot be
    # reconstructed from anything else here - the order's price afterwards is the price
    # the LAST call set, not the price this one asked for.
    "pricing_apply": frozenset({"order_id", "new_price"}),
    # --- fraud package (adapters/driven/tools/fraud/tools.py) ---
    #
    # `account_id` is an identifier for the same reason `order_id` is: it names the
    # account, and the holder's name and risk score are the tool's RESULT, not its
    # argument. Freezing an account is the highest-risk action in this deployment and a
    # trail that cannot say WHICH account was frozen is not evidence of anything.
    "account_history": frozenset({"account_id"}),
    "freeze_account": frozenset({"account_id"}),
    # `case_id` names the case; the note body does not travel with it - see
    # ARGUMENT_PRESENCE_ONLY below.
    "case_notes_append": frozenset({"case_id"}),
    # `query` IS THE ACTION, which is why this one is by value despite being free text a
    # model wrote. For every other tool the name plus the identifiers describe what
    # happened; for `sql_readonly` the statement is the only description that exists, and
    # this is the tool its own module calls the most dangerous in the project. Presence
    # here would render `query = <present, value not recorded>` on an incident row, which
    # is the "we do not know" answer this table exists to prevent.
    #
    # The counter-argument is real and loses on purpose: a WHERE clause can embed personal
    # data. What it cannot embed is anything the tool would have run - `sql_readonly`
    # refuses everything but a bare SELECT against one fixture table before the query
    # reaches data at all. A production replica changes the risk, not this column: it adds
    # a read-only ROLE, a timeout and a row cap, and if a deployment ever needs the
    # statement itself withheld, the honest move is to move this field to presence here
    # and accept that its trail no longer says what was asked.
    "sql_readonly": frozenset({"query"}),
    # --- shared A2A tool (adapters/driven/tools/peers.py), served by t-f11-34's package ---
    #
    # `target` names WHICH agent this one delegated to. A peer's answer is untrusted
    # content (non-negotiable #10) and the profile's two-sided allowlist and hop limit are
    # both decisions about this field, so an incident that starts "an agent was misled by
    # another agent" begins by asking which one - and only this column can answer.
    #
    # `question` is by value for the reason `sql_readonly.query` is: it IS the action.
    # Every other tool is described by its name plus its identifiers; a delegation is
    # described by nothing but what was asked, and "asked billing_specialist something" is
    # the "we do not know" answer. It is also the half of the exchange this deployment
    # WROTE - the answer comes back untrusted and is not an argument here at all.
    "ask_peer": frozenset({"target", "question"}),
    # --- glazed package (adapters/driven/tools/glazed/tools.py) ---
    #
    # Everything the model passes is an identifier, a date, a bounded number or a metric
    # name: what the call was about, and none of it personal data. The store, the case and
    # the date are bound by the turn, never arguments. Tools that take no model argument
    # (`get_order_plan`, `get_supplier_performance`, `get_history`) are decided with an
    # empty set: there is nothing to record, and that is a decision, not a gap.
    "get_snapshot": frozenset(),
    "get_briefing": frozenset(),
    "get_kpis": frozenset({"date_from", "date_to"}),
    "get_day_summary": frozenset({"date"}),
    "get_issues": frozenset({"limit", "category", "kind"}),
    "explain_metric": frozenset({"metric"}),
    "get_order_plan": frozenset(),
    "project_inventory": frozenset({"sku", "horizon"}),
    "forecast_demand": frozenset({"sku", "target_date"}),
    "forecast_daily_flow": frozenset({"target_date"}),
    "get_supplier_performance": frozenset(),
    "get_history": frozenset(),
    "evaluate_promo": frozenset({"promo_id"}),
    "recall_experiences": frozenset({"limit"}),
    "record_event": frozenset({"type", "date_from", "date_to"}),
    # `issue_id` and `option_id` ARE the action: the backend copies action_type, params and
    # tier from the stored option, so these two lines the row up against what was proposed.
    "propose_action": frozenset({"issue_id", "option_id"}),
    "offer_surplus": frozenset({"sku"}),
    "request_stock": frozenset({"sku"}),
    # `decision_id` names the decision checked; `kind` is one of two fixed labels; the
    # department and the discount are the promo being audited. None is personal data.
    "check_compliance": frozenset({"decision_id"}),
    "classify_text": frozenset({"kind"}),
    "audit_promo": frozenset({"department", "discount_pct"}),
    "get_integrity_issues": frozenset(),
}

# Per-tool fields recorded BY PRESENCE: the key is stored with `PRESENCE_RECORDED` in
# place of the value, so the trail can say the argument was passed without keeping what
# was in it. A field in neither map is dropped entirely and the row cannot distinguish it
# from one the model never sent - which is the right outcome for a credential and the
# wrong one for an action somebody has to account for.
#
# A field listed in BOTH maps is stored by value; VALUE already answers PRESENCE.
ARGUMENT_PRESENCE_ONLY: Mapping[str, frozenset[str]] = {
    # `note` is free text the model wrote about a person under fraud investigation. What
    # an operator needs from the trail is that a note was appended and to which case; the
    # text itself already lives in the case file, under the retention rules that file has,
    # and copying it into an append-only table that outlives the case duplicates personal
    # data into the one place it can never be corrected or removed (non-negotiable #6 -
    # this row is written once and never updated). Presence keeps the fact, not the copy.
    "case_notes_append": frozenset({"note"}),
    # Free text the model wrote: its existence matters to the trail, its copy does not
    # (the proposal row and the event row in the backend hold the text under their own
    # retention). `query` is a natural-language memory search that may quote a person.
    "propose_action": frozenset({"rationale"}),
    "record_event": frozenset({"note"}),
    "recall_experiences": frozenset({"query"}),
    # The manager's own words (a rejection reason) or a deviation note: kept in the
    # backend under its retention, not copied into the append-only trail.
    "classify_text": frozenset({"text"}),
}

_LOG = logging.getLogger(__name__)

# `reason` is migration 0022's column (audit_reason_migration.py, t-f11-07). It is the
# SENTENCE the winning rule gave - what the model is handed as the tool result on DENY and
# what the human is asked on NEEDS_APPROVAL - and it is written here, on the same INSERT,
# because a second statement would be a second chance for the evidence to be the half that
# did not land.
#
# `tenant_id` is migration 0023's column (audit_tenant_migration.py, t-f11-21). It rides
# the SAME INSERT for the same reason, and it is taken from the `CallerIdentity` this call
# was decided for - never from `turns`. That table is written inside the domain
# transaction, so a rolled-back turn has these rows and no turn row at all, and a join for
# the tenant would drop exactly the evidence #6 exists to keep.
_INSERT_TOOL_CALL = (
    "INSERT INTO audit_tool_calls "
    "(turn_id, caller, tenant_id, tool, arguments, effect, rule_id, reason) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)
_INSERT_HUMAN_DECISION = (
    "INSERT INTO audit_human_decisions (turn_id, tool_call_id, subject_id, approved, note) "
    "VALUES (%s, %s, %s, %s, %s)"
)
_INSERT_REJECTED_DECISION = (
    "INSERT INTO audit_rejected_decisions (turn_id, tool_call_id, subject_id, reason) "
    "VALUES (%s, %s, %s, %s)"
)
_INSERT_MEDIA = (
    "INSERT INTO audit_media (turn_id, media_id, sha256, direction) VALUES (%s, %s, %s, %s)"
)
_INSERT_TURN_COST = (
    "INSERT INTO audit_turn_costs (turn_id, input_tokens, output_tokens, cached, cost_usd) "
    "VALUES (%s, %s, %s, %s, %s)"
)


def _jsonb(payload: dict[str, object]) -> object:
    """Wrap a mapping for a jsonb column.

    Imported lazily so the module stays importable - and the redaction testable - without
    a compiled psycopg on the machine.
    """
    from psycopg.types.json import Jsonb

    return Jsonb(payload)


class PgAuditSink:
    """`AuditSink` over Postgres, writing on a connection source of its own.

    INSERT only. There is no method here that can rewrite a row, which is what makes the
    append-only rule real rather than aspirational.
    """

    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        argument_allowlist: Mapping[str, frozenset[str]] | None = None,
        argument_presence_only: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        """`connect` is this sink's own connection source - non-negotiable #6.

        The two redaction seats are ONE decision in two shapes: what is stored by value
        and what is stored by presence. Each defaults to its module constant, which is
        what `composition.build_container` gets - it passes neither, so the constants ARE
        the shipped redaction policy and a test that overrides them proves nothing about
        this deployment. An embedding host that registers its own tools overrides BOTH or
        neither; overriding one leaves the other reading a map of tool names it has never
        heard of, which is harmless but says something the caller did not mean.
        """
        self._connect = connect
        self._argument_allowlist: Mapping[str, frozenset[str]] = (
            ARGUMENT_ALLOWLIST if argument_allowlist is None else dict(argument_allowlist)
        )
        self._argument_presence_only: Mapping[str, frozenset[str]] = (
            ARGUMENT_PRESENCE_ONLY
            if argument_presence_only is None
            else dict(argument_presence_only)
        )
        # Tool names already warned about, so a turn that calls an undecided tool forty
        # times leaves one line and not forty. Per instance, never global: a process that
        # builds a second sink with a different map is asking a different question.
        self._undecided_tools_warned: set[str] = set()

    async def record_tool_call(
        self,
        turn_id: TurnId,
        caller: CallerIdentity,
        tool_name: str,
        arguments: dict[str, object],
        decision: PolicyDecision,
    ) -> None:
        """The intent and the verdict, appended before the side effect runs.

        `rule_id` travels with the row: it is the field that answers "why was this
        allowed?" six months later, and it is the only one that cannot be reconstructed
        from anything else here.

        SO DOES `caller.tenant_id`, AND UNTIL t-f11-21 THERE WAS NOWHERE TO PUT IT. The
        row could be scoped to a turn and to nothing else, and the tenant was recoverable
        only by joining `turns` - which is written inside the domain transaction, so a
        turn whose domain writes rolled back has these rows and no turn row to join to.
        The tenant is taken from the identity the decision on this row was made for and
        written on this statement; see audit_tenant_migration.py for what a NULL means.

        SO DOES `decision.reason`, AND UNTIL t-f11-07 IT DID NOT. The id names the rule;
        the reason is what the rule SAID, and the two are not the same evidence. A rule's
        text changes, so resolving the id against `policy_rules` months later answers with
        today's wording - which is exactly the wording that differs on the day somebody
        edits a rule to explain an incident. The sentence is also the only part a human
        ever read: on DENY the model got it back as the tool result, on NEEDS_APPROVAL it
        was the ask somebody answered.

        IT IS STORED VERBATIM AND IS NOT REDACTED, for the reason `record_rejected_decision`
        gives: unlike the arguments it is written by this codebase - the grounds - not by a
        caller. That is also why nothing here assembles a reason out of the call. The
        arguments have their own column and their own per-tool allowlist; a reason built
        from the payload would route a credential around that allowlist into free text no
        redaction ever looks at, and an audit row is not a place to spill a payload.
        """
        await self._append(
            _INSERT_TOOL_CALL,
            (
                str(turn_id),
                caller.subject_id,
                str(caller.tenant_id),
                tool_name,
                _jsonb(self._redact(tool_name, arguments)),
                str(decision.effect.value),
                decision.rule_id,
                decision.reason,
            ),
        )

    async def record_human_decision(
        self,
        turn_id: TurnId,
        tool_call_id: ToolCallId,
        subject_id: str,
        approved: bool,
        note: str | None,
    ) -> None:
        await self._append(
            _INSERT_HUMAN_DECISION,
            (str(turn_id), str(tool_call_id), subject_id, approved, note),
        )

    async def record_rejected_decision(
        self,
        turn_id: TurnId,
        tool_call_id: ToolCallId,
        subject_id: str,
        reason: str,
    ) -> None:
        """The attempt that was refused, on its own row and in its own table.

        Same connection source and same append-only rule as every other member here
        (CLAUDE.md non-negotiable #6): this is the ONLY evidence that the four-eyes
        control fired, so a rollback that takes it makes an enforced rule
        indistinguishable from one that was never wired.

        `reason` is stored verbatim and is not redacted. Unlike tool arguments it is
        written by this codebase - the grounds for the refusal - not by a caller, and it
        is the only part of the row that answers the question the row is read for.
        """
        await self._append(
            _INSERT_REJECTED_DECISION,
            (str(turn_id), str(tool_call_id), subject_id, reason),
        )

    async def record_media(self, turn_id: TurnId, media: MediaRef, direction: str) -> None:
        """The sha256, never the bytes."""
        await self._append(
            _INSERT_MEDIA, (str(turn_id), str(media.media_id), media.sha256, direction)
        )

    async def record_turn_end(self, turn_id: TurnId, usage: Usage, cost_usd: Decimal) -> None:
        """The cost series that is the only place a bad compaction strategy shows up."""
        await self._append(
            _INSERT_TURN_COST,
            (
                str(turn_id),
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_tokens > 0,
                cost_usd,
            ),
        )

    def _redact(self, tool_name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        """Keep the fields this tool opted in; drop everything else.

        Three outcomes per field, and the third is the one t-f11-35 added. A field in the
        VALUE map is stored as it arrived. A field in the PRESENCE map is stored as
        `PRESENCE_RECORDED`, so the row says it was passed without keeping what was in it
        - and a field the model did not pass stays absent either way, because presence
        means presence and inventing the key would assert an argument nobody sent. A field
        in neither map is dropped whole.

        WHAT A NEW TOOL GETS, AND WHY IT IS SAID OUT LOUD
            Nothing, until somebody decides. That is deliberate - a tool added next month
            may take a credential, and a default that stored its arguments would leak it
            on the first call, which is exactly the failure an allowlist exists to make
            impossible. The row is still written, because the caller, the tool and the
            verdict are the evidence; the arguments are a bonus and a leaked credential
            is not.

            But fail-closed-and-QUIET is the state this anchor exists to fix: `{}` shipped
            for eleven waves and nothing ever said so, and the trail read as "the model
            passed nothing" rather than "nobody decided". So the drop is logged once per
            tool name - the NAME only, never a key and never a value, because the reason
            this branch was taken is that nothing here is known to be safe to record.
        """
        allowed = self._argument_allowlist.get(tool_name, frozenset())
        presence = self._argument_presence_only.get(tool_name, frozenset())
        decided = tool_name in self._argument_allowlist or tool_name in self._argument_presence_only
        if not decided and tool_name not in self._undecided_tools_warned:
            self._undecided_tools_warned.add(tool_name)
            _LOG.warning(
                "No audit argument decision for tool %r: every argument of this call is "
                "dropped and its trail will read 'arguments: none recorded'. Add the tool "
                "to ARGUMENT_ALLOWLIST (by value) or ARGUMENT_PRESENCE_ONLY (by presence) "
                "in %s.",
                tool_name,
                __name__,
            )
        redacted: dict[str, object] = {}
        for name, value in arguments.items():
            if name in allowed:
                redacted[name] = value
            elif name in presence:
                redacted[name] = PRESENCE_RECORDED
        return redacted

    async def _append(self, sql: str, params: Sequence[Any]) -> None:
        """One INSERT on this sink's own connection, off the event loop.

        D13: Postgres access is synchronous, so the blocking work runs in a thread. The
        connection comes from `self._connect` and from nowhere else, so the write can
        never join the caller's transaction.
        """
        await asyncio.to_thread(self._append_sync, sql, params)

    def _append_sync(self, sql: str, params: Sequence[Any]) -> None:
        with self._connect() as connection:
            connection.execute(sql, params)
