"""THE CONTRACT TEST. Do not weaken it to make a change pass.

    Adding a vertical is one profile file plus one tools package.
    Zero changes to domain/, application/ or ports/.

This is the single test that keeps the architecture from eroding one convenient exception
at a time. If it fails, the port cut is wrong - fix the design, not the test.
"""

import pytest

CORE_PACKAGES = ("agent_core/domain", "agent_core/application", "agent_core/ports")


@pytest.mark.contract
@pytest.mark.skip(reason="F4 - enable with the first vertical")
def test_adding_a_vertical_does_not_touch_the_core() -> None:
    """PSEUDO-CODE - implement in F4.

    1. git diff --name-only <base>..HEAD
    2. Assert no path matches CORE_PACKAGES.
    3. Skip when the diff is empty (nothing to judge).

    Deliberately a git-level check rather than an import check: it catches a change to a
    domain dataclass made "just to add one field for the new vertical", which is exactly
    how this kind of contract dies.
    """


@pytest.mark.contract
@pytest.mark.skip(reason="F1 - enable once domain and ports are populated")
def test_domain_and_application_import_nothing_external() -> None:
    """PSEUDO-CODE - implement in F1.

    Walk the AST of every module under domain/ and application/. Assert every import is
    stdlib or agent_core.{domain,ports}.

    ruff's banned-api catches the four named libraries; this catches the fifth one nobody
    thought to ban.
    """
