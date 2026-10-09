"""Vertical: fraud analyst tools.

Phase:   after F4 - the second vertical is the REAL proof the core did not change
Tasks:   docs/TASKS.md#t-later-02
Per-vertical: YES
Status:  IMPLEMENTED - four fixture-backed tools, explicitly registered
Covers:  Core/tests/unit/test_fraud_vertical.py

TOOLS
    sql_readonly(query)                  read-only, against a fixture "replica"
    account_history(account_id)          read-only
    case_notes_append(case_id, note)     mutating, low risk
    freeze_account(account_id)           MUTATING, HIGH RISK - always requires approval

WHY THIS IS DELIBERATELY UNLIKE `tools/delivery/tools.py`
    Same shape - the `NO AUTO-DISCOVERY` list, a fixture catalog, `Decimal`-free but no less
    real - and a genuinely different domain: an account is frozen or it is not, a case note
    is appended or it is not, and a query is refused before it ever touches the fixture data.
    Nothing here is copied from delivery's module; where a mechanism would have to be
    (`request_evidence`, `ask_peer`) it already lives in a shared module instead
    (`adapters/driven/tools/evidence.py`, `.../peers.py`) and this package imports it rather
    than growing its own copy - the mistake docs/TASKS.md's F7 note names twice already.

sql_readonly IS THE MOST DANGEROUS TOOL IN THE PROJECT, IN PRODUCTION - NOT HERE
    The model writes the query, and a production version needs three layers:
      1. A database role that physically cannot write. Not a convention - a GRANT.
      2. A statement timeout and a row cap. An unbounded query from a model is a denial of
         service against your own warehouse.
      3. The result passes through the untrusted-content wrapper: rows contain user-supplied
         text, which is an indirect prompt-injection vector straight from your own database.

    This proof-of-seam version has none of a real database to misuse: `_TRANSACTIONS` is a
    fixed, in-process fixture, the same "stand-in, named as one" choice delivery's `_ORDERS`
    makes, for the same reason - there is no `TransactionLedger` port, because CLAUDE.md's
    contract for a vertical is one profile file plus one tools package, not a new port, and
    wiring this vertical to a real warehouse is vertical-owned integration work. What DOES
    survive the fixture, because it costs nothing to keep honest, is the shape of layer 1:
    `sql_readonly` parses its own argument and refuses anything that is not a bare `SELECT`
    against the one servable table, the same discipline a read-only role would enforce by
    construction. Layers 2 and 3 are infrastructure and runner concerns respectively, not
    something a fixture can demonstrate, and are not claimed here.

AUDIT IS EVIDENCE HERE, NOT TELEMETRY
    Retention policy, and possibly a legal reader. Confirm the retention requirement before
    this vertical goes anywhere near production data. Nothing below writes to `AuditSink`
    itself - `PydanticAgentRunner` already records every tool call name and args for every
    vertical (CLAUDE.md non-negotiable #6); a second write from inside a tool body would be
    a second, driftable copy of the same fact.

NO AUTO-DISCOVERY (CLAUDE.md #8, docs/TASKS.md#t-f1-07)
    `build_toolset()` names its four functions in a literal list, exactly as
    `tools/delivery/tools.py` does and for the identical reason: adding a fifth function to
    this file does not add a fifth tool, only editing the list below does.

THE ACCEPTANCE CRITERION FOR THIS WHOLE TASK
    The vertical works end to end AND the diff touches no file under domain/, application/
    or ports/. tests/unit/test_fraud_vertical.py enforces both halves. If either fails, the
    port cut is wrong - stop and revisit it before continuing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from pydantic_ai.toolsets import FunctionToolset


class UnknownAccountError(ValueError):
    """`account_id` does not exist in the fraud fixture catalog.

    Raised, never a sentinel, for the same reason `tools/delivery/tools.py`'s
    `UnknownOrderError` is: a bad id from the model must surface as a tool-call error the
    agent can see and react to, not quote a risk score for an account that was never opened.
    """


class MutatingQueryError(ValueError):
    """`sql_readonly` was handed anything other than a bare `SELECT` on `transactions`.

    See the module docstring's "sql_readonly IS THE MOST DANGEROUS TOOL" section - this is
    the one layer of that defence a fixture can actually enforce: refuse before the query
    ever reaches the fixture data, exactly as a read-only database role would refuse before
    the query ever reached a real one.
    """


@dataclass(frozen=True, slots=True)
class _Account:
    account_id: str
    holder_name: str
    risk_score: int
    frozen: bool


@dataclass(frozen=True, slots=True)
class _Transaction:
    tx_id: str
    account_id: str
    amount_usd: str
    memo: str


# Fixture data for the second vertical - see the module docstring's "sql_readonly" section
# for why this is a fixture and not a real warehouse. Keyed by account_id so a lookup never
# has to scan.
_ACCOUNTS: dict[str, _Account] = {
    "acc-1": _Account(
        account_id="acc-1", holder_name="J. Alvarez", risk_score=72, frozen=False
    ),
    "acc-2": _Account(
        account_id="acc-2", holder_name="R. Chen", risk_score=18, frozen=False
    ),
}

_TRANSACTIONS: dict[str, tuple[_Transaction, ...]] = {
    "acc-1": (
        _Transaction(tx_id="tx-1", account_id="acc-1", amount_usd="4200.00", memo="wire out"),
        _Transaction(tx_id="tx-2", account_id="acc-1", amount_usd="4200.00", memo="wire out"),
    ),
    "acc-2": (
        _Transaction(tx_id="tx-3", account_id="acc-2", amount_usd="35.20", memo="groceries"),
    ),
}

# Case notes are the one genuinely append-only piece of state here - a list a model can grow
# but never edit or delete through this toolset, matching `case_notes_append`'s own name.
_CASE_NOTES: dict[str, list[str]] = {}

_SERVABLE_TABLE = "transactions"
_WRITE_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "truncate",
    "grant",
    "create",
    "merge",
)


def _account(account_id: str) -> _Account:
    account = _ACCOUNTS.get(account_id)
    if account is None:
        raise UnknownAccountError(f"No such account: {account_id!r}")
    return account


def sql_readonly(query: str) -> dict[str, str]:
    """Run a read-only query against the fixture replica. MODEL-WRITTEN ARGUMENT.

    Refuses anything that is not a bare `SELECT` against the one servable table, and
    anything containing a write-shaped keyword even inside a `SELECT` (a subquery smuggling
    a CTE-based write is still refused). See the module docstring for what a production
    version adds on top of this - a database role, a timeout, a row cap and the
    untrusted-content wrapper - none of which a fixture can demonstrate.
    """
    lowered = query.strip().lower()
    if not lowered.startswith("select"):
        raise MutatingQueryError(
            f"sql_readonly only accepts a SELECT statement, got: {query!r}"
        )
    if any(keyword in lowered for keyword in _WRITE_KEYWORDS):
        raise MutatingQueryError(f"sql_readonly refuses a write-shaped query: {query!r}")
    if _SERVABLE_TABLE not in lowered:
        raise ValueError(
            f"no such table on this fixture replica (only {_SERVABLE_TABLE!r} is servable): "
            f"{query!r}"
        )

    rows = [tx for txs in _TRANSACTIONS.values() for tx in txs]
    return {"query": query, "row_count": str(len(rows))}


def account_history(account_id: str) -> dict[str, str]:
    """Return the account's holder, risk score, freeze status and transaction count."""
    account = _account(account_id)
    tx_count = len(_TRANSACTIONS.get(account_id, ()))
    return {
        "account_id": account.account_id,
        "holder_name": account.holder_name,
        "risk_score": str(account.risk_score),
        "frozen": str(account.frozen),
        "transaction_count": str(tx_count),
    }


def case_notes_append(case_id: str, note: str) -> dict[str, str]:
    """Append a note to a case. Mutating, low risk - no approval rule needed for this one."""
    stripped = note.strip()
    if not stripped:
        raise ValueError("note must not be empty")
    notes = _CASE_NOTES.setdefault(case_id, [])
    notes.append(stripped)
    return {"case_id": case_id, "note_count": str(len(notes))}


def freeze_account(account_id: str) -> dict[str, str]:
    """Freeze the account. MUTATING, HIGH RISK - the profile's `approval_rules` always gate
    this one; by the time this function body runs, a human has already approved it, exactly
    as `tools/delivery/tools.py`'s `pricing_apply` docstring describes for its own gate.
    """
    account = _account(account_id)
    _ACCOUNTS[account_id] = replace(account, frozen=True)
    return {"account_id": account.account_id, "frozen": "True"}


def build_toolset() -> FunctionToolset[None]:
    """The fraud vertical's toolset - exactly these four names, explicitly listed.

    Fresh per call, same reasoning as `tools/delivery/tools.py::build_toolset`: nothing here
    holds mutable registration state a caller could share across profiles or turns.
    """
    return FunctionToolset(
        [
            sql_readonly,
            account_history,
            case_notes_append,
            freeze_account,
        ]
    )
