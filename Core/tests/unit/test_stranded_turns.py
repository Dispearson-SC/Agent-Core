"""The operator check for turns a version bump left behind (D30).

DBOS recovers a PENDING workflow only when its row names the application version the
recovering process claims. After `PINNED_DBOS_APPLICATION_VERSION` moves, turns suspended under
the old version are never recovered - and, on a partitioned queue, hold their session's only
slot. This command lists them and gates a deploy script on the answer.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent_core.adapters.driving.cli import stranded_turns
from agent_core.adapters.driving.cli.stranded_turns import StrandedWorkflow
from agent_core.composition import Settings

CURRENT = "agent-core-turns.5"


def _row(workflow_id: str = "wf-1", version: str = "agent-core-turns.1") -> StrandedWorkflow:
    return StrandedWorkflow(
        workflow_id=workflow_id,
        name="run_turn_workflow",
        status="PENDING",
        application_version=version,
        queue_name="agent-core-turns",
        created_at=datetime(2026, 10, 11, 9, 30, tzinfo=UTC),
    )


def test_the_system_database_is_the_one_dbos_derives_from_the_app_url() -> None:
    url = stranded_turns.system_conninfo("postgresql://u:p@h:5432/app_db")
    assert "dbname=app_db_dbos_sys" in url and "host=h" in url
    keyword = stranded_turns.system_conninfo("host=h dbname=app_db user=u")
    assert "dbname=app_db_dbos_sys" in keyword


def test_nothing_stranded_exits_zero_and_says_so() -> None:
    lines: list[str] = []

    code = stranded_turns.run_stranded_check(
        Settings(dbos_application_version=CURRENT),
        find=lambda settings: (),
        write_line=lines.append,
    )

    assert code == 0
    assert any(CURRENT in line and "no stranded" in line.lower() for line in lines)


def test_stranded_turns_are_listed_with_a_remedy_and_exit_non_zero() -> None:
    lines: list[str] = []

    code = stranded_turns.run_stranded_check(
        Settings(dbos_application_version=CURRENT),
        find=lambda settings: (_row("wf-a"), _row("wf-b", "agent-core-turns.2")),
        write_line=lines.append,
    )

    text = "\n".join(lines)
    assert code == 1
    assert "wf-a" in text and "wf-b" in text
    assert "agent-core-turns.1" in text and "agent-core-turns.2" in text
    assert "docs/OPERATIONS.md" in text, "every failing check names what to do about it"


def test_a_check_that_cannot_run_is_not_reported_as_clean() -> None:
    lines: list[str] = []

    def unreachable(settings: Settings) -> tuple[StrandedWorkflow, ...]:
        raise stranded_turns.StrandedCheckUnavailableError("could not connect")

    code = stranded_turns.run_stranded_check(
        Settings(dbos_application_version=CURRENT), find=unreachable, write_line=lines.append
    )

    assert code == 2
    assert "could not connect" in "\n".join(lines)


@pytest.mark.parametrize("secret", ["hunter2"])
def test_the_report_never_prints_the_connection_string(secret: str) -> None:
    lines: list[str] = []

    stranded_turns.run_stranded_check(
        Settings(
            app_conninfo=f"postgresql://u:{secret}@h:5432/app", dbos_application_version=CURRENT
        ),
        find=lambda settings: (_row(),),
        write_line=lines.append,
    )

    assert secret not in "\n".join(lines)


def test_the_subcommand_is_wired_and_its_code_is_the_process_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core import main

    monkeypatch.setattr(main, "run_stranded_check", lambda settings: 1)

    with pytest.raises(SystemExit) as exit_info:
        main.main(["stranded-turns"])

    assert exit_info.value.code == 1


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("connection refused\nHINT: is the server running?", "connection refused"),
        ("  \n  ", ""),
        ("", ""),
        ("one line", "one line"),
    ],
)
def test_the_first_line_of_a_driver_error_is_all_the_report_shows(
    message: str, expected: str
) -> None:
    assert stranded_turns.first_line(message) == expected


def test_a_stranded_workflow_row_converts_the_epoch_milliseconds_of_the_dbos_column() -> None:
    """`dbos.workflow_status.created_at` is BIGINT epoch MILLISECONDS."""
    row = (
        "wf-1", "run_turn_workflow", "PENDING", "agent-core-turns.1", "agent-core-turns",
        1_791_000_000_000,
    )  # fmt: skip

    converted = stranded_turns.row_to_stranded(row)

    assert converted.created_at == datetime.fromtimestamp(1_791_000_000, tz=UTC)
    assert converted.queue_name == "agent-core-turns"
    assert stranded_turns.row_to_stranded((*row[:4], None, row[5])).queue_name is None
