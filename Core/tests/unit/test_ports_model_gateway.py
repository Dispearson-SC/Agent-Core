"""ModelGateway port shape - the day-2 seam frozen as a type.

Phase:   F1 - Real hexagonal core
Tasks:   docs/TASKS.md#t-f1-10

This port exists for one migration: LiteLLM in-process today, LiteLLM as a separate proxy
with virtual keys and per-tenant budgets tomorrow. `ports/model_gateway.py` promises that
migration is "A CHANGE OF base_url AND NOTHING ELSE". Two properties have to hold for that
promise to survive contact with a caller, and neither of them is behaviour - both are shape.

1. `base_url()` LIVES ON THE PORT, NOT ON CALLERS. The moment a use case can reach the
   proxy address any other way - a settings object, an environment read, a constructor
   argument it forwards - day 2 stops being a config edit and becomes a refactor. So the
   test below type-checks a caller that only ever asks the port, and asserts the negative
   case: a gateway missing `base_url` MUST be rejected by the protocol. A protocol that
   accepts a gateway without the day-2 knob is not guarding anything.

2. `RecoveryStrategy` CARRIES EXACTLY THE MEMBERS THE PORT DECLARES. The enum is a
   deliberate five, not Hermes' twenty-five reasons, and the class docstring is where that
   five is argued. Docstring and members are therefore the same fact written twice (D24),
   so the test reads the declaration out of the docstring and compares it to the members
   rather than trusting a hand-copied literal. A sixth member added without an incident to
   justify it, or a documented strategy that no longer exists, both fail here.

3. EVERY IMPLEMENTATION AND EVERY FAKE FOLLOWS THE PORT WHEN IT MOVES. `t-later-03`
   widened `classify_error` to take a `ModelAttempt`, and the stand-ins did not follow:
   one production adapter satisfied the new shape while the fakes still declared the old
   one, so `ModelGateway` meant two different things depending on which module you read.
   The test below type-checks the REAL adapter source and the REAL fakes source against
   the port - not hand-copied stubs, which would only prove a copy is conformant - and
   locks the widening with the `t-f1-05` precedent: the NEGATIVE case is the PRE-widening
   signature, because a lock that only checked the new shape would accept the old one too.

The structural half runs mypy in a subprocess over a throwaway module, the same way the
`AgentRunner` port test does: `inspect` sees names and arities, never whether the
assignment `x: ModelGateway = Stub()` is legal, and that assignment is the only property
the rest of the codebase relies on.

The methods are deliberately SYNC. All three are pure lookups over configuration already in
memory - no I/O to await - which is the same reasoning D13 applies to `ToolPolicy.decide`.
Making them async would put an `await` on the hot path of every model call for nothing.
"""

from __future__ import annotations

import inspect
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agent_core.ports.model_gateway import ModelGateway, RecoveryStrategy

CORE_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = CORE_DIR / "src"
FAKES_MODULE = CORE_DIR / "tests" / "fakes" / "ports.py"

# One assignment per real ModelGateway in the tree. Each is APPENDED to the module that
# actually defines the class, so what gets type-checked is the shipped implementation and
# not a stub that merely resembles it.
_PRODUCTION_ADAPTER = """
from __future__ import annotations

from agent_core.adapters.driven.llm_litellm.gateway import LiteLLMGateway
from agent_core.ports.model_gateway import ModelGateway

gateway: ModelGateway = LiteLLMGateway()
"""

_FAKE_ASSIGNMENT = """
from agent_core.ports.model_gateway import ModelGateway as _ModelGateway

gateway: _ModelGateway = FakeModelGateway()
"""

CONFORMING_STUB = """
from __future__ import annotations

from agent_core.ports.model_gateway import ModelAttempt, ModelGateway, RecoveryStrategy


class StubGateway:
    def model_id_for(self, profile_model: str) -> str:
        raise NotImplementedError

    def base_url(self) -> str | None:
        raise NotImplementedError

    def classify_error(self, error: Exception, attempt: ModelAttempt) -> RecoveryStrategy:
        raise NotImplementedError


gateway: ModelGateway = StubGateway()


def caller_reads_the_proxy_address_from_the_port(gateway: ModelGateway) -> str | None:
    # The whole day-2 payoff: a caller learns where the model lives by asking the port.
    return gateway.base_url()
"""

STUB_WITHOUT_BASE_URL = """
from __future__ import annotations

from agent_core.ports.model_gateway import ModelAttempt, ModelGateway, RecoveryStrategy


class GatewayWithoutBaseUrl:
    def model_id_for(self, profile_model: str) -> str:
        raise NotImplementedError

    def classify_error(self, error: Exception, attempt: ModelAttempt) -> RecoveryStrategy:
        raise NotImplementedError


gateway: ModelGateway = GatewayWithoutBaseUrl()
"""


def _type_check(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Type-check `source` as a standalone module against the real port.

    Written outside the repository tree on purpose: a fixture that deliberately fails to
    type-check must never be picked up by the project-wide mypy run.
    """
    module = tmp_path / "snippet.py"
    module.write_text(source, encoding="utf-8")

    env = dict(os.environ)
    env["MYPYPATH"] = str(SRC_DIR)

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            "--no-error-summary",
            str(module),
        ],
        capture_output=True,
        text=True,
        cwd=str(CORE_DIR),
        env=env,
        check=False,
    )


def _strategies_declared_in_the_docstring() -> frozenset[str]:
    """The strategy names the port argues for, read out of RecoveryStrategy's docstring.

    The docstring is the only place the "five, not twenty-five" argument is made, so it is
    the declaration; the members are the implementation of it. Reading rather than
    hand-copying is what makes the two unable to drift apart (D24).
    """
    doc = inspect.getdoc(RecoveryStrategy) or ""
    return frozenset(re.findall(r"^([A-Z][A-Z_]{2,}) {2,}\S", doc, flags=re.MULTILINE))


@pytest.mark.phase("F1")
def test_recovery_strategy_members_are_exactly_the_strategies_the_port_declares() -> None:
    declared = _strategies_declared_in_the_docstring()
    members = frozenset(RecoveryStrategy.__members__)

    assert declared, (
        "RecoveryStrategy's docstring no longer declares any strategy. That docstring is "
        "where the deliberate five is argued; without it the enum is just a list."
    )
    assert members == declared, (
        "RecoveryStrategy's members drifted from the strategies its docstring declares.\n"
        f"  declared but missing: {sorted(declared - members)}\n"
        f"  present but undeclared: {sorted(members - declared)}\n"
        "A sixth strategy is added only when an incident produces one, and it is argued in "
        "the docstring in the same commit."
    )


@pytest.mark.phase("F1")
def test_every_recovery_strategy_is_a_stable_wire_value() -> None:
    """The strategies are persisted and logged, so the values are part of the contract."""
    for name, member in RecoveryStrategy.__members__.items():
        assert isinstance(member.value, str), f"{name} must carry a string value"
        assert member.value == name.lower(), (
            f"RecoveryStrategy.{name} must serialise as {name.lower()!r}, not {member.value!r}"
        )


@pytest.mark.phase("F1")
def test_classify_error_hands_back_the_port_s_own_strategy_enum() -> None:
    """A caller must not be left to interpret a provider error itself.

    `classify_error` returning `RecoveryStrategy` is what stops a use case from matching on
    a status code - the thing the port docstring warns is not normalised across providers.
    """
    signature = inspect.signature(ModelGateway.classify_error)

    assert signature.return_annotation is RecoveryStrategy or (
        signature.return_annotation == "RecoveryStrategy"
    ), f"classify_error must return RecoveryStrategy, not {signature.return_annotation!r}"


@pytest.mark.phase("F1")
def test_a_caller_asking_the_port_for_base_url_type_checks(tmp_path: Path) -> None:
    result = _type_check(CONFORMING_STUB, tmp_path)

    assert result.returncode == 0, (
        "A gateway carrying the settled signatures must satisfy ModelGateway, and a caller "
        "must be able to read base_url() straight off the port.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F1")
def test_a_gateway_without_base_url_is_not_a_model_gateway(tmp_path: Path) -> None:
    result = _type_check(STUB_WITHOUT_BASE_URL, tmp_path)

    assert result.returncode != 0, (
        "ModelGateway accepted a gateway with no base_url(). Then the proxy address has to "
        "come from somewhere else, and day 2 stops being a one-line config change."
    )
    assert "Incompatible types in assignment" in result.stdout, (
        "Expected the assignment to ModelGateway to be the rejected expression.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F1")
def test_base_url_is_optional_because_day_one_has_no_proxy() -> None:
    """None in library mode, a URL in proxy mode - one return value, the entire migration."""
    annotation = inspect.signature(ModelGateway.base_url).return_annotation

    assert annotation in ("str | None", str | None), (
        f"base_url must return `str | None`, not {annotation!r}. A non-optional return "
        "would force day-1 library mode to invent a URL it does not have."
    )


@pytest.mark.phase("F1")
@pytest.mark.parametrize("name", ["model_id_for", "base_url", "classify_error"])
def test_the_gateway_is_sync_because_nothing_here_does_io(name: str) -> None:
    """D13's `ToolPolicy.decide` reasoning applies verbatim: pure lookups stay sync."""
    member = getattr(ModelGateway, name)

    assert not inspect.iscoroutinefunction(member), (
        f"ModelGateway.{name} is a pure lookup over configuration already in memory. "
        "Making it async puts an await on the hot path of every model call for nothing."
    )


# --- The widening, and the lock that stops it being undone ---------------------------

PRE_WIDENING_STUB = """
from __future__ import annotations

from agent_core.ports.model_gateway import ModelGateway, RecoveryStrategy


class GatewayClassifyingOnTheErrorAlone:
    def model_id_for(self, profile_model: str) -> str:
        raise NotImplementedError

    def base_url(self) -> str | None:
        raise NotImplementedError

    def classify_error(self, error: Exception) -> RecoveryStrategy:
        raise NotImplementedError


gateway: ModelGateway = GatewayClassifyingOnTheErrorAlone()
"""


def _model_gateway_sources() -> dict[str, str]:
    """Every ModelGateway in the tree, as a module that assigns it to the port.

    The fakes module is read from disk and extended rather than imported, exactly as
    `test_fakes.py` does: a name-level import would make a missing class a collection
    error instead of a red assertion, and a hand-written copy would prove nothing about
    the fake the rest of the suite actually injects.
    """
    return {
        "LiteLLMGateway": _PRODUCTION_ADAPTER,
        "FakeModelGateway": FAKES_MODULE.read_text(encoding="utf-8") + _FAKE_ASSIGNMENT,
    }


@pytest.mark.phase("F1")
@pytest.mark.parametrize("implementation", sorted(_model_gateway_sources()))
def test_every_gateway_implementation_and_fake_takes_the_attempt(
    implementation: str, tmp_path: Path
) -> None:
    """A port that moved and a stand-in that did not is a port with two meanings.

    `t-later-03` widened `classify_error` because, with one provider, ROTATE_KEY ("this
    credential is the problem") and FALLBACK_MODEL ("this route is the problem") are
    indistinguishable without the attempt. A fake still declaring the old signature makes
    every use case tested against it pass while the production seat cannot be filled.
    """
    result = _type_check(_model_gateway_sources()[implementation], tmp_path)

    assert result.returncode == 0, (
        f"{implementation} does not satisfy ModelGateway. `classify_error` takes "
        "`(error, attempt: ModelAttempt)` since t-later-03; an implementation or fake "
        "left on the old shape means the port means one thing in src/ and another in "
        "tests/.\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.phase("F1")
def test_a_gateway_classifying_on_the_error_alone_is_not_a_model_gateway(
    tmp_path: Path,
) -> None:
    """The negative case IS the pre-widening shape - that is what makes this a lock.

    Per the `t-f1-05` precedent: asserting only that the widened signature is accepted
    would pass just as happily against the old one, because widening a parameter list is
    invisible to a check that never tries the narrower form.
    """
    result = _type_check(PRE_WIDENING_STUB, tmp_path)

    assert result.returncode != 0, (
        "ModelGateway accepted a gateway whose classify_error takes only the exception. "
        "The widening has been undone, and with it the only evidence that separates a "
        "dead credential from a dead route."
    )
    assert "Incompatible types in assignment" in result.stdout, (
        "Expected the assignment to ModelGateway to be the rejected expression.\n"
        f"{result.stdout}{result.stderr}"
    )
