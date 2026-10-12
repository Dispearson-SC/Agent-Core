"""Driving adapter: the operator's way to forget remembered peer answers (C25, D57).

Tests:   Core/tests/unit/test_peer_answer_cache.py
Used by: agent_core/main.py - the `peer-cache` subcommand
Runbook: docs/OPERATIONS.md, "The peer answer cache"

    python -m agent_core peer-cache purge --tenant <tenant> [--target <agent>]

Forgets everything remembered for the tenant, or only the answers of one target agent, and says how
many entries went. A tenant is required: one command can never clear another tenant's entries. This
is the hook for "the data behind these answers just changed"; the other three are the TTL, a changed
profile (its content hash is part of every key) and `AGENT_CORE_PEER_CACHE_DATA_VERSION`.

EXIT CODE: 0 done (including "nothing to forget"), 2 the cache could not be reached.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable

import psycopg

from agent_core.adapters.driven.peers.answer_cache import PgAnswerCache
from agent_core.composition import Settings
from agent_core.ports.answer_cache import AnswerCache

__all__ = ["add_peer_cache_parser", "run_peer_cache"]


def add_peer_cache_parser(subcommands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subcommands.add_parser(
        "peer-cache",
        help="forget remembered peer answers (see OPERATIONS.md, 'The peer answer cache')",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    purge = actions.add_parser("purge", help="forget a tenant's remembered answers")
    purge.add_argument("--tenant", required=True, help="the tenant whose entries to forget")
    purge.add_argument("--target", default=None, help="only the answers of this target agent")


def run_peer_cache(
    arguments: argparse.Namespace,
    settings: Settings,
    *,
    cache: AnswerCache | None = None,
    out: Callable[[str], None] = print,
) -> int:
    """Run the parsed `peer-cache` command. `cache` is injectable for tests; by default it is
    the Postgres cache on its own short-lived connections (this is a one-shot command)."""
    store = cache or PgAnswerCache(lambda: psycopg.connect(settings.app_conninfo))
    scope = f"tenant {arguments.tenant!r}" + (
        "" if arguments.target is None else f", target {arguments.target!r}"
    )
    try:
        removed = asyncio.run(store.invalidate(arguments.tenant, arguments.target))
    except Exception as error:
        out(f"could not reach the peer answer cache: {type(error).__name__}")
        return 2
    out(f"forgot {removed} remembered peer answer(s) for {scope}")
    return 0
