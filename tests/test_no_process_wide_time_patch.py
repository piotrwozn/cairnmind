"""No test replaces a function of the ``time`` module for the whole process (#417)."""
from __future__ import annotations

import ast
from pathlib import Path
import re

# ``monkeypatch.setattr(module.time, "sleep", ...)`` changes the one ``time`` module every thread and every module shares:
# in server mode the other code of the process sleeps too, and each of those calls ends in the stand-in.
TARGET = re.compile(r"(?:^|\.)time\.\w+$")
PATCHERS = {"setattr", "patch", "object"}  # monkeypatch.setattr, mock.patch, patch.object


def _is_time(node, aliases):
    """``time``, a name bound to it, ``x.time`` or ``getattr(x, "time")``."""
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Attribute):
        return node.attr == "time"
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
            and len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and node.args[1].value == "time")


def process_wide_patches(source):
    """The line numbers where ``source`` replaces something of the shared ``time`` module."""
    tree, aliases = ast.parse(source), {"time"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "time")
        elif isinstance(node, ast.Assign) and _is_time(node.value, aliases):
            aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args and getattr(node.func, "attr", getattr(node.func, "id", "")) in PATCHERS:
            first = node.args[0]
            if _is_time(first, aliases) or (isinstance(first, ast.Constant) and isinstance(first.value, str)
                                            and TARGET.search(first.value)):
                found.append(node.lineno)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and _is_time(t.value, aliases) for t in node.targets):
            found.append(node.lineno)
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == "time" \
                and getattr(node.value, "attr", "") == "modules":
            found.append(node.lineno)
    return found


def test_no_test_patches_an_attribute_of_a_modules_time():
    found = [f"{path.name}:{number}" for path in sorted(Path(__file__).parent.rglob("*.py")) if path != Path(__file__)
             for number in process_wide_patches(path.read_text(encoding="utf-8"))]
    assert not found, (
        f"{', '.join(found)} patch an attribute of a module's `time`, which is the shared `time` module: it changes every "
        "sleep of the process, the engine's and other threads' too. Write `own_time(monkeypatch, module, sleep=...)` "
        "(tests/conftest.py): only that module's own `time` name is replaced.")


def test_the_guard_finds_what_it_is_for():
    for source in ('monkeypatch.setattr(cli.time, "sleep", f)', 'patch.object(engine.time, "sleep", f)',
                   'monkeypatch.setattr("cairnmind.cli.time.sleep", f)', 'monkeypatch.setattr(time, "sleep", f)',
                   'mock.patch("time.sleep")', 'with patch("time.sleep", f): pass', 'monkeypatch.setattr("time.sleep", f)',
                   'setattr(getattr(cli, "time"), "sleep", f)', 't = cli.time\nsetattr(t, "sleep", f)',
                   'import time as clock\nsetattr(clock, "sleep", f)', 'monkeypatch.setattr(\n    cli.time, "sleep", f)',
                   'sys.modules["time"] = fake', 'time.sleep = f', 'cli.time.sleep = f'):
        assert process_wide_patches(source), source
    for source in ('monkeypatch.setattr(agent_module, "time", SimpleNamespace(monotonic=clock))', 'x = self.time, 3',
                   'own_time(monkeypatch, cli, sleep=f)', 'monkeypatch.setattr("cairnmind.cli.datetime.now", f)',
                   'time.sleep(1)'):
        assert not process_wide_patches(source), source
