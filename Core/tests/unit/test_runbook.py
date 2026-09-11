"""The operator runbook is checked, not trusted. docs/TASKS.md#t-f11-31.

Phase:   F11 - A clone, an empty Postgres, and one command
Tasks:   docs/TASKS.md#t-f11-31
Status:  the assertion this file makes

WHY THIS EXISTS
    `README.md` is prose an operator reads and then TYPES. This repository has already had
    to retract two documents that were read as authority and were wrong (docs/FIELD-NOTES.md,
    RETRACTED). A runbook nothing checks goes stale at the first rename and is then worse
    than nothing, because a reader trusts it.

WHAT THIS DOES NOT DO
    It does not connect to a database, start a process, or import anything that needs one -
    this is a unit test (Makefile: "no database, no network, no model"). It reads
    `README.md` as text and cross-checks two closed, checkable claims against the source
    that would actually make them true:

    1. every `python -m agent_core <word>` the runbook names is a real subcommand of
       `agent_core.main`'s own argument parser - not a word that used to be one.
    2. every SHOUTING_SNAKE_CASE token the runbook shows in backticks is an environment
       variable some code path in this tree actually reads - not a name that drifted from
       a rename, and not an invented-looking example.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_core.main import _argument_parser

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"
CORE_SRC = REPO_ROOT / "Core" / "src" / "agent_core"

# An inline code span that looks like an environment variable: SHOUTING_SNAKE_CASE with at
# least one underscore, so ordinary shouted words the runbook also uses in backticks -
# `SQL`, `HTTP`, `YAML`, `TDD` - never match. A real variable name always has one.
_ENV_LOOKING_TOKEN = re.compile(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`")

# `python -m agent_core <word>` inside a code span, anywhere in the runbook.
_SUBCOMMAND_MENTION = re.compile(r"`python -m agent_core ([a-z][a-z-]*)")

# A credential travels as `PROVIDER/model` in a profile's `model:` field and litellm's own
# convention - restated in adapters/driven/llm_litellm/models.py - is that its credential
# variable is `<PROVIDER>_API_KEY`. Deriving the expected name from the profiles actually
# shipped (rather than hand-listing "MINIMAX_API_KEY") means a renamed or added provider
# cannot silently drift out of what this test accepts.
_PROFILE_MODEL_LINE = re.compile(r"^model:\s*([a-z][a-z0-9]*)/\S+\s*$", re.MULTILINE)

# `gemini/gemini-3-flash-preview` is not shipped by any profile, but it is a provider this
# tree documents as a verified, supported model - see ports/embedder.py and
# adapters/driven/llm_litellm/gateway.py, which name it explicitly as one of the two
# providers docs/STATE.md lists as live. The runbook is allowed to mention its credential
# because the SOURCE, not the runbook, is what claims Gemini is a real option here.
_DOCUMENTED_PROVIDER_MODEL = re.compile(r"`([a-z][a-z0-9]*)/gemini-[a-z0-9.-]+`")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _real_subcommands() -> set[str]:
    """Every subcommand `python -m agent_core` actually dispatches. Not a hand-kept list."""
    help_text = _argument_parser().format_help()
    # `peer-worker` has a hyphen, and the first version of this pattern did not. A guard
    # that cannot SEE a subcommand silently stops guarding it: the runbook could name
    # anything hyphenated and this test would keep passing, which is the exact drift it
    # exists to catch.
    match = re.search(r"\{([a-z,-]+)\}", help_text)
    assert match is not None, "argument parser grew no subcommand group at all"
    return set(match.group(1).split(","))


def _env_vars_the_code_reads() -> set[str]:
    """Every environment variable name some module in `agent_core` actually looks up.

    Four shapes, because the tree resolves variables four different ways and each is a
    real, separate way for the runbook to go stale:

      - `Settings.from_env` reading its own merged mapping (`env.get("NAME"...)`)
      - `main.py` reading `os.environ` directly for the two the container never sees
        (`_HOST_ENV`, `_PORT_ENV`)
      - a model credential, named by litellm's own `<PROVIDER>_API_KEY` /
        `<PROVIDER>_API_BASE` convention, for every provider this tree actually ships a
        profile for or documents as supported
      - the day-2 LiteLLM proxy variables the integration suite itself reads
        (`_env_or_dotenv("NAME")`) - real names in this tree even though `Settings` gains
        no field for them until D2
    """
    composition_src = _read(CORE_SRC / "composition.py")
    main_src = _read(CORE_SRC / "main.py")
    integration_dir = REPO_ROOT / "Core" / "tests" / "integration"

    names = set(re.findall(r"""env\.get\(\s*["']([A-Z][A-Z0-9_]*)["']""", composition_src))
    names.update(
        re.findall(r"""_(?:HOST|PORT)_ENV\s*=\s*["']([A-Z][A-Z0-9_]*)["']""", main_src)
    )
    for test_file in integration_dir.glob("*.py"):
        names.update(
            re.findall(r"""_env_or_dotenv\(\s*["']([A-Z][A-Z0-9_]*)["']""", _read(test_file))
        )

    # `_MAPPED_CREDENTIAL_NAMES = {"MINIMAX_API": "MINIMAX_API_KEY"}` - both sides are real
    # variable names: the file-side one `.env.example` carries, and the wire-side one
    # litellm actually reads once `_export_mapped_credentials` has run.
    mapped_block = re.search(
        r"_MAPPED_CREDENTIAL_NAMES.*?\{(.*?)\}", composition_src, re.DOTALL
    )
    assert mapped_block is not None, "composition.py dropped _MAPPED_CREDENTIAL_NAMES"
    names.update(re.findall(r'"([A-Z][A-Z0-9_]*)"', mapped_block.group(1)))

    profiles_dir = REPO_ROOT / "Core" / "profiles"
    providers = {
        match.group(1)
        for profile in profiles_dir.glob("*.yaml")
        for match in _PROFILE_MODEL_LINE.finditer(_read(profile))
    }
    # Gemini: not shipped, but named in source as a supported, verified provider (see the
    # docstring above `_DOCUMENTED_PROVIDER_MODEL`).
    for source_file in ("ports/embedder.py", "adapters/driven/llm_litellm/gateway.py"):
        providers.update(
            match.group(1)
            for match in _DOCUMENTED_PROVIDER_MODEL.finditer(_read(CORE_SRC / source_file))
        )
    # Both suffixes are real, code-derived conventions - `models.py` builds both
    # `{provider.upper()}_API_KEY` and `{provider.upper()}_API_BASE` in its own refusal
    # messages, for the credential and the endpoint override respectively.
    for provider in providers:
        names.add(f"{provider.upper()}_API_KEY")
        names.add(f"{provider.upper()}_API_BASE")

    return names


def test_every_subcommand_the_runbook_names_is_a_real_subcommand() -> None:
    mentioned = set(_SUBCOMMAND_MENTION.findall(_read(README)))
    assert mentioned, "the runbook names no `python -m agent_core <word>` at all"
    assert mentioned <= _real_subcommands(), (
        f"README.md names a subcommand agent_core.main does not dispatch: "
        f"{mentioned - _real_subcommands()}"
    )


def test_every_environment_variable_the_runbook_names_is_one_the_code_reads() -> None:
    mentioned = set(_ENV_LOOKING_TOKEN.findall(_read(README)))
    assert mentioned, "the runbook names no environment variable at all"
    known = _env_vars_the_code_reads()
    assert mentioned <= known, (
        f"README.md names an environment variable no code path reads: {mentioned - known}"
    )


def test_the_runbook_never_shows_a_credential_shaped_value() -> None:
    """Names only. Not even an example that looks like one - the anchor's own words.

    A connection string with a filled-in user:password (even the harmless local default)
    is exactly the shape someone copy-pastes without changing, so it is banned as a shape,
    not only as a real secret.
    """
    text = _read(README)
    assert not re.search(r"://[^/\s]*:[^/\s@]*@", text), (
        "README.md shows a credential-shaped connection string; describe the variable by "
        "name instead"
    )
