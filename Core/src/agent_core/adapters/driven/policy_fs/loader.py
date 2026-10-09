"""Driven adapter: policy rules as reviewed configuration on disk.

Phase:      F11
Tasks:      docs/TASKS.md#t-f11-03
Implements: no port - see WHY THERE IS NO PORT HERE.

SILENT-BUG AREA (CLAUDE.md). A hole in the policy engine never fails a test; it only ever
fails on the day it matters. Everything below is written for a human reading a diff,
because a human reading a diff is the only check these rules will ever get.

WHAT WAS ACTUALLY BROKEN
    Nothing in production wrote `policy_rules`. Only tests did. So a fresh deployment came
    up with an empty table, `PgToolPolicy` correctly denied every tool, and there was no
    supported way to change that: the first person to use this system inserted rules with
    raw SQL at a psql prompt. The table existed, the reducer worked, the migration had run
    - and the system was unusable, with nothing failing anywhere.

    The same shape as the rest of F11: the fixture that supplies a missing collaborator is
    exactly what stops anyone noticing it is missing.

WHY A FILE AND NOT A CONSOLE COMMAND
    A `:allow <tool>` command is the widening CLAUDE.md non-negotiable #9 forbids. The
    console runs as a `CallerIdentity` and writing policy is an ADMINISTRATIVE act; a
    command that let a chat identity grant itself a tool would put the two on one route,
    which is the one thing `AdminIdentity` exists to prevent.

    So rules live in `Core/policy/*.yaml` and are applied at startup the way a migration
    is: declarative, diffable, idempotent, and reviewed BEFORE it is in force. The cost is
    that changing a rule needs a restart, and that cost is deliberate (docs/ROADMAP.md
    F11): a permission that can be granted at a prompt is a permission granted without a
    record.

THE FILE IS THE WHOLE TRUTH, DELETIONS INCLUDED
    `apply_policy_rules` RECONCILES: it upserts what the files declare and DELETES every
    stored rule they no longer mention. An applier that only ever adds is a permission
    nobody can revoke without the SQL prompt this module exists to remove - and the diff a
    reviewer reads would stop being the set that is in force.

    The reconciliation runs in ONE transaction. A half-applied permission set is the worst
    possible outcome of a crash here: the DELETEs could land while the INSERTs did not,
    and the deployment would come back denying everything with nothing to say why.

WHY THERE IS NO PORT HERE
    Same reasoning as `adapters/driven/profiles_fs/loader.py`. Nothing in `application/`
    asks for the rule FILE: rules are reconciled once at startup by the composition root,
    and every read afterwards goes through `ToolPolicy`, which is already a port. A second
    protocol with exactly one caller that is itself the wiring would be a seam nobody uses.

WHY THE WRITE LIVES BESIDE THE PARSE, IN A MODULE NAMED FOR THE FILESYSTEM
    Because this is a migration, not a repository. `adapters/driven/persistence_pg/
    *_migration.py` set the precedent: the module that owns a change owns both its
    DEFINITION and its APPLICATION, so the two can never disagree about shape. Splitting
    the parse from the write would put the JSON layout in two files, and a duplicated fact
    drifts (CLAUDE.md, Conventions) - here it would drift into rows `PgToolPolicy` silently
    drops as malformed, which reads as a permission that was never granted.

THE STORED SHAPE IS MIGRATION 0004's, PLUS 0012's COLUMN
    `policy_rules (rule_id pk, definition jsonb, tenant_id, updated_at)`. Every field of a
    rule lives inside `definition`; `tenant_id` is additionally written to the column
    because `PgToolPolicy._SELECT_RULES` narrows on `COALESCE(tenant_id,
    definition->>'tenant_id')` and the column is the authoritative half of that pair.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import psycopg
import yaml  # type: ignore[import-untyped]  # no stub package installed; see pyproject.toml
from psycopg.types.json import Jsonb

from agent_core.domain.policy import Effect, PolicyRule
from agent_core.domain.turn import TenantId

__all__ = [
    "PolicyRuleFileError",
    "apply_policy_rules",
    "apply_policy_rules_sync",
    "load_policy_rules",
    "parse_policy_rules",
]


class PolicyRuleFileError(ValueError):
    """A rule file is malformed, or two files declare the same `rule_id`.

    RAISED, never skipped, and it takes startup down with it. That is the deliberate
    choice: a rule this loader cannot read is a permission decision nobody made, and the
    two ways to be wrong are not symmetric. A dropped DENY leaves a tool open and nothing
    says so; a dropped ALLOW leaves an agent mysteriously less capable. Refusing to start
    is the only outcome an operator cannot fail to notice.

    It names the FILE, because a reviewer reading `Core/policy/` needs to know which of
    several documents is the one to fix.
    """


def parse_policy_rules(text: str, *, source: str) -> tuple[PolicyRule, ...]:
    """One rule document to domain rules, in file order.

    `safe_load`, never `load`: this file decides what an agent may do, and a loader that
    can construct arbitrary Python objects turns "someone may edit the policy" into
    "someone may run code in the agent process". Same reasoning as
    `profiles_fs/loader.py::parse_profile` and the note on `ApprovalRule.condition`.

    The document is a mapping with a `rules:` list, never a bare list. The extra key costs
    one line and buys a place to put the next thing a policy file has to say - a version,
    a comment block a tool reads - without changing the shape of every file in the wild.
    """
    document: Any = yaml.safe_load(text)
    if document is None:
        return ()
    if not isinstance(document, Mapping):
        raise PolicyRuleFileError(
            f"{source}: a policy file is a mapping with a `rules:` list, not "
            f"{type(document).__name__}."
        )
    raw = document.get("rules")
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PolicyRuleFileError(f"{source}: `rules` must be a list.")
    return tuple(_to_rule(entry, source=source, position=index) for index, entry in enumerate(raw))


def load_policy_rules(policy_dir: Path) -> tuple[PolicyRule, ...]:
    """Every rule declared under `policy_dir`, in file-name then file order.

    Sorted by file name so the set is reproducible across processes and platforms;
    directory iteration order is not, and the order decides nothing about the verdict
    (`EFFECT_PRECEDENCE` does) but it does decide what a diff of the applied set looks
    like.

    A directory with no files is an empty tuple, not an error. What that MEANS is decided
    by the caller: `composition.start_container` treats an existing-but-empty policy
    directory as an authoritative "no rules", because a reviewer who empties the directory
    has said something, while a directory that is not there at all means this deployment
    does not manage policy from files.

    A `rule_id` declared twice is refused rather than resolved by order: the winner would
    depend on file names, and it decides what an agent is permitted to do - exactly the
    reasoning `composition.load_profiles` gives for duplicate profile ids.
    """
    rules: list[PolicyRule] = []
    seen: dict[str, str] = {}
    for path in sorted(policy_dir.glob("*.yaml")):
        for rule in parse_policy_rules(path.read_text(encoding="utf-8"), source=path.name):
            previous = seen.get(rule.rule_id)
            if previous is not None:
                raise PolicyRuleFileError(
                    f"rule_id {rule.rule_id!r} is declared in both {previous} and "
                    f"{path.name}. Refusing to pick one: the loser's effect would vanish "
                    "silently, and on a DENY that is a permission nobody granted."
                )
            seen[rule.rule_id] = path.name
            rules.append(rule)
    return tuple(rules)


# WHAT IS WRITTEN, AND WHAT IS COMPARED ON THE NEXT START.
#
# `WHERE` on the `DO UPDATE` is what makes a restart a true no-op rather than a no-op that
# touches every row: without it `updated_at` moves on every deploy, and a column whose
# whole job is to say WHEN a permission last changed would answer "at the last restart"
# forever after. `IS DISTINCT FROM` rather than `<>` because either side can be NULL -
# `tenant_id` usually is, and `NULL <> NULL` is NULL, which is not a change.
_UPSERT_RULE = """
    INSERT INTO policy_rules (rule_id, definition, tenant_id, updated_at)
    VALUES (%s, %s, %s, now())
    ON CONFLICT (rule_id) DO UPDATE
        SET definition = EXCLUDED.definition,
            tenant_id  = EXCLUDED.tenant_id,
            updated_at = now()
        WHERE policy_rules.definition IS DISTINCT FROM EXCLUDED.definition
           OR policy_rules.tenant_id  IS DISTINCT FROM EXCLUDED.tenant_id
"""

# THE HALF THAT MAKES THE FILE REVIEWABLE. A rule the files no longer declare is removed,
# so the diff a reviewer reads IS the set in force afterwards. `<> ALL` over an empty array
# is TRUE for every row, which is the correct reading of an emptied policy directory: the
# reviewer deleted every rule, and every rule stops applying.
_DELETE_UNDECLARED = "DELETE FROM policy_rules WHERE rule_id <> ALL(%s)"


def apply_policy_rules_sync(app_conninfo: str, rules: Iterable[PolicyRule]) -> None:
    """Reconcile `policy_rules` with `rules`. Idempotent, atomic, and it DELETES.

    The sync island. Postgres transactions are sync (D13) and this runs at startup, before
    the process has an event loop worth protecting; `apply_policy_rules` below is the async
    face every caller above the adapter layer uses.

    NOT autocommit, unlike the migration appliers beside it. Those are autocommit because
    `CREATE TABLE IF NOT EXISTS` is independently idempotent and a half-applied schema is
    recoverable by re-running. This is neither: the DELETEs and the INSERTs are one
    decision about what is permitted, and landing half of it would leave a deployment
    denying tools that the reviewed file grants, with no error anywhere to say so.
    """
    declared = tuple(rules)
    with psycopg.connect(app_conninfo) as connection:
        with connection.transaction():
            for rule in declared:
                connection.execute(
                    _UPSERT_RULE,
                    (
                        rule.rule_id,
                        Jsonb(_definition(rule)),
                        None if rule.tenant_id is None else str(rule.tenant_id),
                    ),
                )
            connection.execute(_DELETE_UNDECLARED, ([rule.rule_id for rule in declared],))


async def apply_policy_rules(app_conninfo: str, rules: Iterable[PolicyRule]) -> None:
    """`apply_policy_rules_sync` off the event loop. D13, like every adapter here."""
    declared = tuple(rules)
    await asyncio.to_thread(apply_policy_rules_sync, app_conninfo, declared)


def _definition(rule: PolicyRule) -> dict[str, Any]:
    """The `definition` jsonb, shaped exactly as `PgToolPolicy._to_rule` reads it.

    `subject_roles` and `channels` are SORTED lists rather than the frozensets they are in
    the domain: jsonb preserves array order, so an unsorted set would serialise differently
    between two processes and the `IS DISTINCT FROM` above would report a change that is
    not one - re-stamping `updated_at` on every restart.

    `tenant_id` is omitted when the rule belongs to every tenant, rather than written as a
    JSON null. Both read as "all tenants" (`PgToolPolicy._to_rule` uses `.get`), and
    omitting it keeps the stored document the same shape the table has carried since
    migration 0004.
    """
    definition: dict[str, Any] = {
        "tool_pattern": rule.tool_pattern,
        "effect": rule.effect.value,
        "reason": rule.reason,
        "subject_roles": sorted(rule.subject_roles),
        "channels": sorted(rule.channels),
    }
    if rule.tenant_id is not None:
        definition["tenant_id"] = str(rule.tenant_id)
    return definition


def _to_rule(entry: Any, *, source: str, position: int) -> PolicyRule:
    """One YAML entry to one `PolicyRule`, with every way of being wrong named.

    An absent `subject_roles`, `channels` or `tenant_id` means "any" - the reading
    `domain/policy.py` pins for an empty constraint, restated here only so the failure
    messages can say so to whoever wrote the file.
    """
    where = f"{source}, rule #{position + 1}"
    if not isinstance(entry, Mapping):
        raise PolicyRuleFileError(f"{where}: a rule is a mapping, not {type(entry).__name__}.")

    rule_id = _required_text(entry, "rule_id", where)
    tool_pattern = _required_text(entry, "tool_pattern", where)
    effect_name = _required_text(entry, "effect", where)
    try:
        effect = Effect(effect_name)
    except ValueError as error:
        permitted = ", ".join(member.value for member in Effect)
        raise PolicyRuleFileError(
            f"{where}: effect {effect_name!r} is not one of {permitted}."
        ) from error

    reason = str(entry.get("reason", "")).strip()
    if not reason:
        raise PolicyRuleFileError(
            f"{where}: a rule needs a `reason`. On DENY it is what the model is told, and "
            "on NEEDS_APPROVAL it is what the human is asked (domain/policy.py). A rule "
            "with none leaves both audiences with nothing."
        )

    tenant = entry.get("tenant_id")
    return PolicyRule(
        rule_id=rule_id,
        tool_pattern=tool_pattern,
        effect=effect,
        reason=reason,
        subject_roles=_text_set(entry.get("subject_roles"), "subject_roles", where),
        channels=_text_set(entry.get("channels"), "channels", where),
        tenant_id=None if tenant is None else TenantId(str(tenant)),
    )


def _required_text(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PolicyRuleFileError(f"{where}: `{key}` is required and must be a non-empty string.")
    return value.strip()


def _text_set(value: Any, key: str, where: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise PolicyRuleFileError(
            f"{where}: `{key}` must be a list of strings; an empty list - or the key left "
            "out - means ANY, never NONE (domain/policy.py, PolicyRule.matches)."
        )
    return frozenset(str(item) for item in value)
