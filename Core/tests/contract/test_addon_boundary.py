"""Phase 2 exit criterion (A8, D82): no addon modifies or import-patches `domain/`, `application/`
or `ports/`.

THE CHECK IS STATIC, over every `.py` file under `addons/`. It forbids:
  * importing the protected packages (`agent_core.domain`, `.application`, `.ports`) or anything
    under them, except the ALLOWED SURFACE (`addon_boundary.ALLOWED_SURFACE`);
  * the dynamic routes around an import statement: `__import__`, `importlib`, `import_module`,
    `exec`, `eval`, `sys.modules`, `builtins`.
It is a guard against a careless or lazy addon, not a sandbox: addon code runs in the Core's
process, enabling an addon is an explicit operator decision and addons are reviewed like code
(docs/ADDONS.md, "Limitations"). The second half checks the opposite direction: the inner layers
never import the addon adapter, so nothing in them can depend on an addon.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.contract.addon_boundary import ALLOWED_SURFACE, PROTECTED, scan_directory, scan_source

pytestmark = [pytest.mark.contract, pytest.mark.phase("2")]

REPOSITORY = Path(__file__).resolve().parents[3]
SRC = REPOSITORY / "Core" / "src" / "agent_core"


@pytest.mark.parametrize(
    "source",
    [
        "import agent_core.domain\n",
        "import agent_core.domain.policy as p\n",
        "from agent_core.domain import policy\n",
        "from agent_core.domain.policy import PolicyRule\n",
        "from agent_core.application.run_turn import RunTurn\n",
        "from agent_core.ports.audit import AuditSink\n",
        "from agent_core import domain\n",
        "from agent_core import ports as p\n",
        "import agent_core.ports.tool_policy\n",
        "import agent_core.domain as d\n",
        "from agent_core import domain as d\n",
        "from agent_core import (\n    adapters,\n    ports,\n)\n",
        "from agent_core.domain import *\n",
        "from ..agent_core.domain import policy\n",
    ],
    ids=lambda s: s.strip().replace("\n", " "),
)
def test_the_scan_rejects_every_way_of_importing_a_protected_package(source: str) -> None:
    assert scan_source(source), source


@pytest.mark.parametrize(
    "source",
    [
        "__import__('agent_core.domain.policy')\n",
        "import importlib\n",
        "from importlib import import_module\n",
        "exec('import agent_core.domain')\n",
        "eval('1')\n",
        "import sys\nsys.modules['agent_core.domain.policy'] = object()\n",
        "import builtins\n",
        "from sys import modules\n",
    ],
    ids=lambda s: s.strip().splitlines()[-1],
)
def test_the_scan_rejects_the_dynamic_routes_around_an_import(source: str) -> None:
    assert scan_source(source), source


@pytest.mark.parametrize(
    "source",
    [
        "import agent_core\nx = agent_core.domain.policy.PolicyRule\n",
        "import agent_core as core\ncore.ports.audit_sink\n",
        "import agent_core.adapters\nagent_core.application.run_turn\n",
        "from agent_core import adapters, ports\nadapters.x\n",
        "import sys as s\ns.modules['x'] = 1\n",
        "import builtins as b\n",
        "import importlib as il\nil.import_module('agent_core.domain')\n",
    ],
    ids=lambda s: s.strip().splitlines()[-1],
)
def test_the_scan_rejects_reaching_a_protected_package_without_an_import_of_it(
    source: str,
) -> None:
    assert scan_source(source), source


@pytest.mark.parametrize(
    "source",
    [
        "from pydantic_ai.tools import Tool\n",
        "from agent_core.adapters.driven.addons_fs.install import AddonInstallContext\n",
        "from agent_core.domain.tool_replay import safe_tool_metadata\n",
        "import agent_core.domain.tool_replay\n",
        "from agent_core import adapters\n",
        "import agent_core_addons_helper\n",
        "import json, asyncio\n",
        # Attributes and methods that only share a name with something forbidden:
        "class A:\n    def f(self):\n        return self.exec(1) + self.eval(2)\n",
        "def f(connection):\n    return connection.import_module('x')\n",
        "def f(config):\n    return config.modules, config.domain.policy, config.ports\n",
        "def f(sys):\n    return sys.modules\n",  # a parameter, not the stdlib module
        "def f(s):\n    return s.modules\n",
        "import agent_core.domain.tool_replay as t\nt.safe_tool_metadata()\n",
        "import agent_core.domain.tool_replay\nagent_core.domain.tool_replay.safe_tool_metadata\n",
        "import agent_core\nagent_core.__version__\n",
        "from agent_core import adapters as a\na.domain\n",  # agent_core.adapters.domain
        "from . import helpers\nfrom .sub import x\n",
    ],
    ids=lambda s: s.strip().replace("\n", " "),
)
def test_the_scan_accepts_what_an_addon_may_use(source: str) -> None:
    assert scan_source(source) == [], source


def test_the_allowed_surface_is_small_and_names_only_read_only_authoring_helpers() -> None:
    assert ALLOWED_SURFACE == {"agent_core.domain.tool_replay"}
    assert all(module.startswith(PROTECTED) for module in ALLOWED_SURFACE)


def test_a_violation_names_the_file_the_line_and_the_rule() -> None:
    (message,) = scan_source("x = 1\nfrom agent_core.domain.policy import Effect\n", "shop/t.py")

    assert message.startswith("shop/t.py:2:") and "agent_core.domain.policy" in message


def test_every_python_file_of_every_addon_stays_outside_the_inner_layers() -> None:
    scanned, violations = scan_directory(REPOSITORY / "addons")

    assert scanned >= 4, "the scan must actually read the sample addons' Python"
    assert violations == []


def test_the_scan_reads_nested_directories(tmp_path: Path) -> None:
    (tmp_path / "shop" / "wrappers").mkdir(parents=True)
    (tmp_path / "shop" / "wrappers" / "w.py").write_text("import agent_core.ports\n")
    (tmp_path / "shop" / "README.md").write_text("import agent_core.ports\n")

    scanned, violations = scan_directory(tmp_path)

    assert scanned == 1 and len(violations) == 1 and "w.py" in violations[0]


def _imports(path: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
    return found


@pytest.mark.parametrize("layer", ["domain", "application", "ports"])
def test_the_inner_layers_never_import_the_addon_machinery(layer: str) -> None:
    files = sorted((SRC / layer).rglob("*.py"))
    assert files

    offenders = [
        f"{path.relative_to(SRC)} imports {module}"
        for path in files
        for module in _imports(path)
        if "addons_fs" in module or module.startswith("agent_core_addons")
    ]

    assert offenders == []
