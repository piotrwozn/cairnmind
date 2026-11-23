"""No test replaces a function of the ``time`` module for the whole process (#417)."""
from __future__ import annotations

from pathlib import Path
import re

# ``monkeypatch.setattr(module.time, "sleep", ...)`` changes the one ``time`` module every thread and every module shares:
# in server mode the other code of the process sleeps too, and each of those calls ends in the stand-in.
PROCESS_WIDE = re.compile(r"""(?:setattr|patch\.object)\(\s*(?:\w+\.)*time\s*,|["'](?:\w+\.)+time\.\w+["']""")


def test_no_test_patches_an_attribute_of_a_modules_time():
    found = [f"{path.name}:{number}" for path in sorted(Path(__file__).parent.glob("test_*.py")) if path != Path(__file__)
             for number, line in enumerate(path.read_text().splitlines(), 1) if PROCESS_WIDE.search(line)]
    assert not found, (
        f"{', '.join(found)} patch an attribute of a module's `time`, which is the shared `time` module: it changes every "
        "sleep of the process, the engine's and other threads' too. Write `own_time(monkeypatch, module, sleep=...)` "
        "(tests/conftest.py): only that module's own `time` name is replaced.")


def test_the_guard_finds_what_it_is_for():
    for line in ('monkeypatch.setattr(cli.time, "sleep", f)', 'patch.object(engine.time, "sleep", f)',
                 'monkeypatch.setattr("cairnmind.cli.time.sleep", f)', 'monkeypatch.setattr(time, "sleep", f)'):
        assert PROCESS_WIDE.search(line), line
    for line in ('monkeypatch.setattr(agent_module, "time", SimpleNamespace(monotonic=clock))', 'x = self.time, 3'):
        assert not PROCESS_WIDE.search(line), line
