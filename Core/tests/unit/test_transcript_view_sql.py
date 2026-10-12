"""C1n: the conversations view definition is its own constant, not a slice of another migration.

Migration 0035 used to cut `transcript_conversations` out of migration 0016's text from a marker to
the END of the string, so any statement later appended to 0016 would be re-run by 0035.
"""

from __future__ import annotations

from agent_core.adapters.driven.persistence_pg.peer_owner_migration import PEER_OWNER_MIGRATION
from agent_core.adapters.driven.persistence_pg.transcript_migration import (
    TRANSCRIPT_CONVERSATIONS_VIEW_SQL,
    TRANSCRIPT_ENTRIES_VIEW_SQL,
    TRANSCRIPT_PROJECTION_MIGRATION,
)


def test_the_conversations_view_is_one_statement_of_its_own() -> None:
    sql = TRANSCRIPT_CONVERSATIONS_VIEW_SQL.strip()

    assert sql.startswith("CREATE OR REPLACE VIEW transcript_conversations")
    assert ";" not in sql, "a single statement, so a caller can place its own terminator"


def test_migration_0016_is_built_from_the_two_view_constants() -> None:
    sql = TRANSCRIPT_PROJECTION_MIGRATION.sql

    assert TRANSCRIPT_ENTRIES_VIEW_SQL in sql
    assert TRANSCRIPT_CONVERSATIONS_VIEW_SQL in sql


def test_migration_0035_recreates_the_view_and_runs_nothing_after_it() -> None:
    sql = PEER_OWNER_MIGRATION.sql

    assert sql.count("CREATE OR REPLACE VIEW transcript_conversations") == 1
    assert sql.rstrip().endswith(";"), "the migration ends with the patched view and nothing else"
    assert "COALESCE(rt.session_id, t.session_id) = s.session_id" in sql


# --- C1o: the newest-first page statement is written out, not derived by string replacement ---
# CHARACTERIZATION: these pass before and after the refactor of `_PAGE_NEWEST_FIRST_SQL` from a
# `str.replace` of `_PAGE_SQL` to an explicit constant. They exist so that a later edit of either
# statement cannot silently flip an order or a comparison (the replace would simply stop matching).


def test_the_oldest_first_page_walks_up_from_its_cursor() -> None:
    from agent_core.adapters.driven.persistence_pg import transcript_repository as repo

    assert "(at, entry_id) > (" in repo._PAGE_SQL  # noqa: SLF001
    assert "ORDER BY at ASC, entry_id ASC" in repo._PAGE_SQL  # noqa: SLF001


def test_the_newest_first_page_walks_down_from_its_cursor() -> None:
    from agent_core.adapters.driven.persistence_pg import transcript_repository as repo

    sql = repo._PAGE_NEWEST_FIRST_SQL  # noqa: SLF001
    assert "(at, entry_id) < (" in sql and "(at, entry_id) > (" not in sql
    assert "ORDER BY at DESC, entry_id DESC" in sql and "ASC" not in sql


def test_the_two_page_statements_differ_only_in_comparison_and_order() -> None:
    from agent_core.adapters.driven.persistence_pg import transcript_repository as repo

    def shape(sql: str) -> str:
        return (
            " ".join(sql.split())
            .replace("(at, entry_id) > (", "(at, entry_id) ? (")
            .replace("(at, entry_id) < (", "(at, entry_id) ? (")
            .replace("ORDER BY at ASC, entry_id ASC", "ORDER BY ?")
            .replace("ORDER BY at DESC, entry_id DESC", "ORDER BY ?")
        )

    assert shape(repo._PAGE_SQL) == shape(repo._PAGE_NEWEST_FIRST_SQL)  # noqa: SLF001
