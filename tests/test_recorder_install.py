"""The cairnmind-recorder subagent that install writes for Claude Code only (#376). No engine."""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

from cairnmind import integrations, mcp_server, recorder_agent, sessions, terminal_i18n
from cairnmind.config import Settings
from cairnmind.mcp_server import ServiceClient

PATH = recorder_agent.RECORDER_PATH


@pytest.fixture
def settings(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in integrations.SESSION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    loaded = Settings.load(tmp_path / "memory", create=True)
    loaded.ask_timeout = 60.0
    return loaded


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return {"id": "project-a", "name": "Example", "root": str(root)}


@pytest.fixture
def probe(monkeypatch):
    info = {"installed": True, "version": "2.1.319 (Claude Code)", "mcp": True, "hooks": True,
            "packet_passing": True, "session_binding": True, "verified_resume": True, "notes": []}
    monkeypatch.setattr(integrations, "_probe_cli", lambda *args, **kwargs: dict(info))
    return info


def frontmatter(text: str) -> dict:
    head = text.split("\n---\n", 1)[0].removeprefix("---\n")
    values = {}
    for line in head.splitlines():
        match = re.match(r"^\s*(\w+): (.*)$", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def test_install_writes_the_recorder_for_claude_code_only(settings, project, probe):
    root = Path(project["root"])
    result = integrations.install_integrations(settings, project)
    path = root / PATH
    assert path.read_text(encoding="utf-8") == recorder_agent.agent_file(settings, project)
    assert str(path) in result["changed"] and result["clients"]["claude"]["recorder_configured"] is True
    assert result["clients"]["codex"]["recorder_configured"] is False
    assert not (root / ".codex" / "agents").exists()
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
    only_codex = integrations.install_integrations(settings, project, ("codex",))
    assert not any(PATH in item for item in only_codex["changed"])


def test_a_second_install_changes_nothing_and_another_data_dir_rewrites_the_file(settings, project, probe, tmp_path):
    root = Path(project["root"])
    integrations.install_integrations(settings, project)
    again = integrations.install_integrations(settings, project)
    assert again["changed"] == []
    settings.data_dir = tmp_path / "elsewhere"
    moved = integrations.install_integrations(settings, project)
    assert str(root / PATH) in moved["changed"]
    assert str(settings.data_dir) in (root / PATH).read_text(encoding="utf-8")
    assert len(list((root / ".claude" / "agents").glob("*.cairnmind-backup-*"))) == 1


def test_a_recorder_file_without_the_markers_is_left_unchanged(settings, project, probe):
    root = Path(project["root"])
    path = root / PATH
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: cairnmind-recorder\n---\nMine.\n", encoding="utf-8")
    result = integrations.install_integrations(settings, project)
    assert path.read_text(encoding="utf-8") == "---\nname: cairnmind-recorder\n---\nMine.\n"
    assert recorder_agent.RECORDER_KEPT.format(path=PATH) in result["warnings"]
    assert result["clients"]["claude"]["recorder_configured"] is False
    path.unlink()
    path.mkdir()  # a directory is not ours either
    assert recorder_agent.RECORDER_KEPT.format(path=PATH) in integrations.install_integrations(settings, project)["warnings"]
    path.rmdir()
    target = root / "elsewhere.md"
    target.write_text(recorder_agent.agent_file(settings, project), encoding="utf-8")
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("no symlinks here")
    assert recorder_agent.RECORDER_KEPT.format(path=PATH) in integrations.install_integrations(settings, project)["warnings"]
    assert path.is_symlink()


def test_a_symlinked_agents_folder_is_never_written_through_or_removed_from(settings, project, probe, tmp_path):
    root, shared = Path(project["root"]), tmp_path / "shared-agents"
    shared.mkdir()
    (root / ".claude").mkdir()
    try:
        (root / ".claude" / "agents").symlink_to(shared, target_is_directory=True)
    except OSError:
        pytest.skip("no symlinks here")
    result = integrations.install_integrations(settings, project)
    assert list(shared.iterdir()) == [] and result["clients"]["claude"]["recorder_configured"] is False
    assert str(root / PATH) not in result["changed"]
    assert recorder_agent.RECORDER_KEPT.format(path=PATH) in result["warnings"]
    (shared / "cairnmind-recorder.md").write_text(recorder_agent.agent_file(settings, project), encoding="utf-8")
    probe["version"] = "2.1.100"
    integrations.install_integrations(settings, project)
    assert [item.name for item in shared.iterdir()] == ["cairnmind-recorder.md"]
    (root / ".claude" / "agents").unlink()
    (root / ".claude").rename(root / "real-claude")
    (root / ".claude").symlink_to(root / "real-claude", target_is_directory=True)
    probe["version"] = "2.1.319"
    integrations.install_integrations(settings, project)
    assert not (root / "real-claude" / "agents" / "cairnmind-recorder.md").exists()


def test_an_older_claude_code_gets_no_recorder_and_loses_the_one_install_wrote(settings, project, probe):
    root = Path(project["root"])
    path = root / PATH
    probe["version"] = "2.1.237 (Claude Code)"
    result = integrations.install_integrations(settings, project)
    assert not path.exists() and result["clients"]["claude"]["recorder_configured"] is False
    assert recorder_agent.RECORDER_NOT_WRITTEN.format(version="2.1.237 (Claude Code)") in result["warnings"]
    probe["version"] = "2.1.319"
    integrations.install_integrations(settings, project)
    assert path.is_file()
    probe["version"] = "2.1.237"
    result = integrations.install_integrations(settings, project)
    assert not path.exists() and str(path) not in result["changed"]
    assert recorder_agent.RECORDER_REMOVED.format(version="2.1.237", path=PATH) in result["warnings"]
    assert len(list(path.parent.glob("*.cairnmind-backup-*"))) == 1
    path.write_text("mine\n", encoding="utf-8")
    result = integrations.install_integrations(settings, project)
    assert path.read_text(encoding="utf-8") == "mine\n" and str(path) not in result["changed"]
    for unparsable in (None, "unknown"):
        path.unlink(missing_ok=True)
        probe["version"] = unparsable
        result = integrations.install_integrations(settings, project)


def test_no_recorder_without_claude_or_without_its_mcp_capability(settings, project, probe):
    root = Path(project["root"])
    probe["installed"] = False
    result = integrations.install_integrations(settings, project, ("claude",))
    assert not (root / ".claude").exists() and result["clients"]["claude"]["recorder_configured"] is False
    probe["installed"], probe["mcp"] = True, False
    result = integrations.install_integrations(settings, project, ("claude",))
    assert not (root / PATH).exists() and result["clients"]["claude"]["recorder_configured"] is False
    assert not any("cairnmind-recorder" in warning for warning in result["warnings"])
    probe["mcp"] = True
    integrations.install_integrations(settings, project, ("claude",))
    probe["mcp"] = False
    result = integrations.install_integrations(settings, project, ("claude",))  # an earlier file is named, not hidden
    assert (root / PATH).is_file() and recorder_agent.RECORDER_LEFT.format(path=PATH) in result["warnings"]


def test_the_recorder_file_names_only_its_own_server_and_tools(settings, project):
    text = recorder_agent.agent_file(settings, project)
    values = frontmatter(text)
    assert values["name"] == "cairnmind-recorder" and "model" not in values
    assert tuple(values["tools"].split(", ")) == recorder_agent.RECORDER_TOOLS
    assert not any(name.endswith("__checkpoint") for name in recorder_agent.RECORDER_TOOLS)  # it would re-mark native fields
    assert all(name.startswith("mcp__cairnmind-recorder__") for name in recorder_agent.RECORDER_TOOLS)
    args = json.loads(values["args"])
    assert args[:5] == ["-I", "-m", "cairnmind", "--data-dir", str(settings.data_dir)]
    assert args[-5:] == ["mcp", "--client", "external", "--project", project["id"]]
    assert json.loads(values["command"]) == sys.executable and values["type"] == "stdio"
    assert json.loads(values["env"]) == {name: "" for name in integrations.SESSION_ENV_VARS}
    description = json.loads(values["description"])
    assert description == recorder_agent.RECORDER_DESCRIPTION and len(description.encode("utf-8")) <= 600
    body = text.split("\n---\n", 1)[1]
    assert body.startswith(integrations.BEGIN + "\n") and body.endswith(integrations.END + "\n")
    assert body.count(integrations.BEGIN) == body.count(integrations.END) == 1
    assert "Not recorded: " in body and integrations.DATA_NOT_INSTRUCTIONS in body
    assert recorder_agent.RECORDER_REPLY.format(kind="<kind>", id="<id>", version="<version>",
                                                session="<origin_session_id>") in body
    assert project["id"] in body and "{project}" not in body


def test_an_external_bridge_starts_where_the_session_variables_are_empty_and_names_no_other_session(
        settings, monkeypatch):
    for name in integrations.SESSION_ENV_VARS:
        monkeypatch.setenv(name, "")
    ServiceClient(settings, external={"project": "p", "worktree": None})
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "x")
    with pytest.raises(ValueError):
        ServiceClient(settings, external={"project": "p", "worktree": None})
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "")
    client = ServiceClient(settings, external={"project": "p", "worktree": None})
    monkeypatch.setattr(mcp_server, "other_version", lambda _settings: None)
    client.session_id = "mine"
    with pytest.raises(RuntimeError, match="Session mismatch"):
        asyncio.run(client.request("POST", "/remember", payload={"session_id": "another", "packet_id": "p"}))


def test_a_new_empty_project_with_the_recorder_is_still_an_empty_managed_scope(settings, project, probe):
    root = Path(project["root"])
    integrations.install_integrations(settings, project)
    eligible, _, paths = sessions._empty_scope(str(root), settings, project)
    assert eligible and str(root / PATH) in paths
    with (root / PATH).open("a", encoding="utf-8") as stream:
        stream.write(" ")
    assert sessions._empty_scope(str(root), settings, project)[0] is False


def test_every_recorder_sentence_has_polish():
    for sentence in recorder_agent.MESSAGES:
        polish = recorder_agent.MESSAGES[sentence]
        assert terminal_i18n.CATALOG[sentence] == polish
        assert set(re.findall(r"\{\w+\}", sentence)) == set(re.findall(r"\{\w+\}", polish))


def test_the_recorders_file_is_known_to_the_managed_file_lists(settings, project, probe):
    from cairnmind import ingest, project_removal, scope
    integrations.install_integrations(settings, project)
    lines = (Path(project["root"]) / PATH).read_text(encoding="utf-8").splitlines()
    assert scope._MANAGED.match_file(PATH) and PATH in project_removal.INSTALLED_FILES
    indexed = "\n".join("\n".join(lines[first:end]) for first, end in ingest._indexed_spans(lines, PATH))
    assert "name: cairnmind-recorder" in indexed and "You are cairnmind-recorder" not in indexed