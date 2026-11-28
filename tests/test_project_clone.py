"""Cloning a GitHub/Git repository via `gh` or `git` in CLI, broker and TUI (fast tier, no ArcadeDB)."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import make_symlink_or_skip
from consent_steps import form_ready
from fastapi.testclient import TestClient
from test_terminal_panels import settle
from test_terminal_panels import setup as setup_panel
from textual.widgets import Input, Static
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.agents import knowledge_mode
from cairnmind.cli_cmds import first_start
from cairnmind.config import Settings
from cairnmind.memory import project_clone, project_removal
from cairnmind.scip import ingest
from cairnmind.storage.store_writer import StoreNotWriter
from cairnmind.terminal.cockpit import create_cockpit_app
from cairnmind.terminal.terminal_commands import COMMANDS, CommandPaletteApp, dispatch_command
from cairnmind.terminal.terminal_i18n import tr
from cairnmind.terminal.terminal_panels import SidebarApp

# --- Repository parsing and safety ----------------------------------------------------------------------------------

@pytest.mark.parametrize(("spec", "kind", "host", "owner", "repo", "git_url", "gh_target"), [
    ("piotrwozn/cairnmind", "github_shorthand", "github.com", "piotrwozn", "cairnmind",
     "https://github.com/piotrwozn/cairnmind.git", "piotrwozn/cairnmind"),
    ("piotrwozn/cairnmind.git", "github_shorthand", "github.com", "piotrwozn", "cairnmind",
     "https://github.com/piotrwozn/cairnmind.git", "piotrwozn/cairnmind"),
    ("https://github.com/piotrwozn/cairnmind", "https", "github.com", "piotrwozn", "cairnmind",
     "https://github.com/piotrwozn/cairnmind", "piotrwozn/cairnmind"),
    ("https://github.com/piotrwozn/cairnmind.git/", "https", "github.com", "piotrwozn", "cairnmind",
     "https://github.com/piotrwozn/cairnmind.git", "piotrwozn/cairnmind"),
    ("git@github.com:piotrwozn/cairnmind.git", "ssh", "github.com", "piotrwozn", "cairnmind",
     "git@github.com:piotrwozn/cairnmind.git", "piotrwozn/cairnmind"),
    ("ssh://git@github.com/piotrwozn/cairnmind.git", "ssh", "github.com", "piotrwozn", "cairnmind",
     "ssh://git@github.com/piotrwozn/cairnmind.git", "piotrwozn/cairnmind"),
    ("git@github-work:org/service.git", "ssh", "github-work", "org", "service",
     "git@github-work:org/service.git", "git@github-work:org/service.git"),
    ("https://gitlab.com/team/sub/service.git", "https", "gitlab.com", "team/sub", "service",
     "https://gitlab.com/team/sub/service.git", "https://gitlab.com/team/sub/service.git"),
    ("ssh://git@git.example.com:2222/team/service.git", "ssh", "git.example.com", "team", "service",
     "ssh://git@git.example.com:2222/team/service.git", "ssh://git@git.example.com:2222/team/service.git"),
])
def test_parse_clone_source_accepts_github_and_other_safe_git_specs(spec, kind, host, owner, repo, git_url, gh_target):
    parsed = project_clone.parse_clone_source(spec)
    assert (parsed.kind, parsed.host, parsed.owner, parsed.repo, parsed.default_name, parsed.git_url, parsed.gh_target) == (
        kind, host, owner, repo, repo, git_url, gh_target)


@pytest.mark.parametrize(("bad", "expected"), [
    ("", "owner/repo"),
    ("   ", "owner/repo"),
    ("-owner/repo", "owner/repo"),
    ("--upload-pack=sh", "owner/repo"),
    ("owner/repo with space", "owner/repo"),
    ("owner/repo\nnext", "owner/repo"),
    ("http://github.com/owner/repo", "http:// is refused"),
    ("file:///tmp/repo", "file:// is refused"),
    ("git://github.com/owner/repo.git", "git:// is refused"),
    ("ext::sh -c id", "ext:: is refused"),
    ("https://ghp_secret@github.com/owner/repo.git", "embedded credentials"),
    ("https://user:pass@github.com/owner/repo.git", "embedded credentials"),
    ("ssh://git:secret@github.com/owner/repo.git", "embedded credentials"),
    ("https://github.com/owner/repo/tree/main", "not a page or path inside it"),
    ("https://github.com/owner/repo?tab=readme", "owner/repo"),
    ("https://github.com/owner/repo#readme", "owner/repo"),
    ("owner/..", "owner/repo"),
    ("../repo", "owner/repo"),
    ("just-a-word", "owner/repo"),
    ("a/b/c", "owner/repo"),
])
def test_parse_clone_source_refuses_unsafe_or_malformed_specs(bad, expected):
    with pytest.raises(ValueError, match=re.escape(expected)):
        project_clone.parse_clone_source(bad)


def test_is_explicit_remote_url_never_matches_shorthand_or_local_paths():
    for yes in ("https://github.com/o/r", "ssh://git@github.com/o/r.git", "git@github.com:o/r.git",
                "git@github-work:org/app.git"):
        assert project_clone.is_explicit_remote_url(yes) is True
    for no in (None, "", "owner/repo", "src/my-app", "/tmp/my-app", "http://github.com/o/r", "file:///tmp/r"):
        assert project_clone.is_explicit_remote_url(no) is False


def test_validate_branch_rejects_options_and_invalid_refs():
    assert project_clone.validate_branch("main") == "main"
    assert project_clone.validate_branch("release/v1.0") == "release/v1.0"
    for bad in ("", "  ", "-b", "--force", "a..b", "a b", "main\nnext"):
        with pytest.raises(ValueError):
            project_clone.validate_branch(bad)


# --- Tool choice: gh vs git -----------------------------------------------------------------------------------------

def test_choose_tool_matrix_for_auto_gh_and_git():
    shorthand = project_clone.parse_clone_source("owner/repo")
    gh_https = project_clone.parse_clone_source("https://github.com/owner/repo")
    gh_ssh = project_clone.parse_clone_source("git@github.com:owner/repo.git")
    alias_ssh = project_clone.parse_clone_source("git@github-work:owner/repo.git")
    gitlab = project_clone.parse_clone_source("https://gitlab.com/team/repo.git")

    both = lambda name: f"/usr/bin/{name}"  # noqa: E731
    only_git = lambda name: "/usr/bin/git" if name == "git" else None  # noqa: E731
    only_gh = lambda name: "/usr/bin/gh" if name == "gh" else None  # noqa: E731

    # git missing always fails (gh repo clone depends on git too)
    with pytest.raises(project_clone.CloneError, match="git was not found"):
        project_clone.choose_tool(shorthand, "auto", which=only_gh, is_gh_authenticated=lambda h: True)

    # auto with gh installed and authenticated: GitHub shorthand & HTTPS use gh; SSH & non-GitHub use git
    assert project_clone.choose_tool(shorthand, "auto", which=both, is_gh_authenticated=lambda h: True) == "gh"
    assert project_clone.choose_tool(gh_https, "auto", which=both, is_gh_authenticated=lambda h: True) == "gh"
    assert project_clone.choose_tool(gh_ssh, "auto", which=both, is_gh_authenticated=lambda h: True) == "git"
    assert project_clone.choose_tool(alias_ssh, "auto", which=both, is_gh_authenticated=lambda h: True) == "git"
    assert project_clone.choose_tool(gitlab, "auto", which=both, is_gh_authenticated=lambda h: True) == "git"

    # auto with gh installed but NOT authenticated falls back to git (so public repos still clone)
    assert project_clone.choose_tool(shorthand, "auto", which=both, is_gh_authenticated=lambda h: False) == "git"
    assert project_clone.choose_tool(gh_https, "auto", which=both, is_gh_authenticated=lambda h: False) == "git"

    # auto with only git installed uses git without probing gh
    probed = []
    assert project_clone.choose_tool(shorthand, "auto", which=only_git,
                                     is_gh_authenticated=lambda h: probed.append(h) or True) == "git"
    assert probed == []

    # explicit --via git / --via gh
    assert project_clone.choose_tool(shorthand, "git", which=both, is_gh_authenticated=lambda h: True) == "git"
    assert project_clone.choose_tool(gh_ssh, "gh", which=both, is_gh_authenticated=lambda h: False) == "gh"
    with pytest.raises(project_clone.CloneError, match="gh was not found"):
        project_clone.choose_tool(shorthand, "gh", which=only_git)
    with pytest.raises(ValueError, match="auto, gh or git"):
        project_clone.choose_tool(shorthand, "svn", which=both)


def test_clone_environment_strips_cairnmind_secrets_and_disables_prompts_when_non_interactive():
    env = {"PATH": "/usr/bin", "SSH_AUTH_SOCK": "/tmp/ssh", "GH_TOKEN": "keep-user-gh",
           "CAIRNMIND_ARCADE_PASSWORD": "secret", "DEVBRAIN_SERVICE_TOKEN": "old-secret"}
    non_tty = project_clone.clone_environment(interactive=False, environ=env)
    assert "CAIRNMIND_ARCADE_PASSWORD" not in non_tty and "DEVBRAIN_SERVICE_TOKEN" not in non_tty
    assert non_tty["SSH_AUTH_SOCK"] == "/tmp/ssh" and non_tty["GH_TOKEN"] == "keep-user-gh"
    assert non_tty["GIT_TERMINAL_PROMPT"] == "0" and non_tty["GH_PROMPT_DISABLED"] == "1"
    tty = project_clone.clone_environment(interactive=True, environ=env)
    assert "CAIRNMIND_ARCADE_PASSWORD" not in tty and "GIT_TERMINAL_PROMPT" not in tty


def test_clone_command_and_secret_scrubbing(tmp_path):
    source = project_clone.parse_clone_source("owner/repo")
    dest = tmp_path / "repo"
    assert project_clone.clone_command(source, dest, "git", "dev") == [
        "git", "clone", "--branch", "dev", "--", "https://github.com/owner/repo.git", str(dest)]
    assert project_clone.clone_command(source, dest, "gh", "dev") == [
        "gh", "repo", "clone", "owner/repo", str(dest), "--", "--branch", "dev"]
    scrubbed = project_clone._scrub_secrets("fatal: https://ghp_1234567890abcdef@github.com/o/r \x1b[31merr")
    assert "ghp_1234567890abcdef" not in scrubbed and "\x1b" not in scrubbed and "[REDACTED]" in scrubbed


# --- Destination and real local bare-repo git clone -----------------------------------------------------------------

def test_prepare_destination_guards_data_dir_credentials_symlinks_and_non_empty(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(ValueError, match="inside the data directory"):
        project_clone.prepare_destination(data / "repo", data)
    (data / "credentials").mkdir()
    (data / "credentials" / "api-keys.json").write_text("{}")
    with pytest.raises(ValueError, match="credential store"):
        project_clone.prepare_destination(tmp_path, data)
    file_target = tmp_path / "file.txt"
    file_target.write_text("x")
    with pytest.raises(ValueError, match="is a file"):
        project_clone.prepare_destination(file_target, data)
    non_empty = tmp_path / "existing"
    non_empty.mkdir()
    (non_empty / "a.txt").write_text("hi")
    with pytest.raises(ValueError, match="not empty"):
        project_clone.prepare_destination(non_empty, data)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert project_clone.prepare_destination(empty, data) == empty.resolve()
    fresh = tmp_path / "fresh" / "sub"
    assert project_clone.prepare_destination(fresh, data) == fresh.resolve(strict=False)


def test_prepare_destination_refuses_symlink(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    link = tmp_path / "link"
    make_symlink_or_skip(link, tmp_path / "nowhere")
    with pytest.raises(ValueError, match="symbolic link"):
        project_clone.prepare_destination(link, data)


def _make_bare_repo(tmp_path: Path, name: str = "demo", *, branch: str = "main",
                    extra_files: dict[str, str] | None = None) -> Path:
    work = tmp_path / f"{name}-src"
    bare = tmp_path / f"{name}.git"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branch, str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "Test"], check=True)
    (work / "app.py").write_text("def greet():\n    return 'hello'\n")
    for rel, content in (extra_files or {}).items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "initial"], check=True)
    subprocess.run(["git", "clone", "--bare", "-q", str(work), str(bare)], check=True)
    return bare


def test_real_git_clone_subprocess_with_branch_and_failure_hints(tmp_path):
    bare = _make_bare_repo(tmp_path, branch="develop")
    source = project_clone.CloneSource(
        raw="owner/demo", kind="github_shorthand", host="github.com", owner="owner", repo="demo",
        default_name="demo", git_url=str(bare), gh_target="owner/demo")
    dest = tmp_path / "cloned"
    info = project_clone.clone_repository(source, dest, via="git", branch="develop", interactive=False)
    assert info["tool"] == "git" and info["branch"] == "develop" and (dest / "app.py").is_file()

    def failing_git(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 128, "", "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n")
    with pytest.raises(project_clone.CloneError) as exc:
        project_clone.clone_repository(
            project_clone.parse_clone_source("owner/private"), tmp_path / "p1", via="git",
            which=lambda name: f"/usr/bin/{name}", runner=failing_git)
    assert "gh auth login" in str(exc.value) and "git@github.com:owner/private.git" in str(exc.value)

    def failing_gh(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "GraphQL: Could not resolve to a Repository\n")
    with pytest.raises(project_clone.CloneError) as gh_exc:
        project_clone.clone_repository(
            project_clone.parse_clone_source("owner/private"), tmp_path / "p2", via="gh",
            which=lambda name: f"/usr/bin/{name}", runner=failing_gh)
    assert "--via git" in str(gh_exc.value) and "gh auth status" in str(gh_exc.value)


# --- CLI: cairnmind project add and cairnmind add -------------------------------------------------------------------

class FakeStore:
    def __init__(self, data_dir: Path, projects=None, *, not_writer=None):
        self.data_dir = data_dir
        self._projects = list(projects or [])
        self.not_writer = not_writer
        self.added = []

    def list_projects(self):
        return list(self._projects)

    def add_project(self, name, root, restore=True):
        if self.not_writer:
            raise StoreNotWriter(self.not_writer)
        row = {"id": f"p-{len(self._projects) + 1}", "name": name, "root": str(root)}
        self._projects.append(row)
        self.added.append((name, str(root), restore))
        return row

    def job_summary(self, project_id):
        return {group: {"pending": 0, "running": 0} for group in ("indexing", "extraction")}


@pytest.fixture
def cli_world(monkeypatch, tmp_path):
    data = tmp_path / "memory"
    store = FakeStore(data)
    cloned_calls = []
    fake_memory = {"snapshot": False, "published": False}

    def fake_clone(source, destination, *, via="auto", branch=None, interactive=False, **kw):
        cloned_calls.append((source.raw, str(destination), via, branch, interactive))
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "app.py").write_text("x = 1\n")
        if fake_memory["snapshot"]:
            (destination / ".memory" / "cairnmind").mkdir(parents=True, exist_ok=True)
            (destination / ".memory" / "cairnmind" / "manifest.json").write_text("{}")
        if fake_memory["published"]:
            (destination / ".memory" / "cairnmind-published").mkdir(parents=True, exist_ok=True)
        return {"source": source.raw, "url": source.git_url, "tool": "gh" if via == "gh" else "git",
                "destination": str(destination), **({"branch": branch} if branch else {})}

    monkeypatch.setattr(cli, "store_for", lambda *a, **k: store)
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(knowledge_mode, "enabled", lambda settings: True)
    monkeypatch.setattr(project_removal, "removed_rows", lambda reader: [])
    monkeypatch.setattr(ingest, "index_project", lambda s, pid: {"files": 1})
    monkeypatch.setattr(ingest, "register_default_histories", lambda *a: {})
    monkeypatch.setattr(project_clone, "choose_tool", lambda source, via="auto", **kw: "gh" if via == "gh" else "git")
    monkeypatch.setattr(project_clone, "clone_repository", fake_clone)
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)

    def invoke(*args, terminal=False):
        monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: terminal)
        return CliRunner().invoke(cli.app, ["--data-dir", str(data), *args])

    return SimpleNamespace(invoke=invoke, store=store, cloned=cloned_calls, cwd=work, data=data, memory=fake_memory)


def test_cli_add_and_project_add_clone_defaults_to_cwd_repo_even_with_custom_name(cli_world):
    # 1) Shortest one-shot: `cairnmind add --clone owner/repo` -> name=repo, root=./repo
    res = cli_world.invoke("add", "--clone", "owner/alpha", terminal=False)
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["project"]["name"] == "alpha"
    assert Path(payload["project"]["root"]) == (cli_world.cwd / "alpha").resolve()
    assert payload["cloned"]["tool"] == "git" and payload["cloned"]["source"] == "owner/alpha"

    # 2) Custom NAME without ROOT -> still clones into ./<repo> (P2)
    res2 = cli_world.invoke("add", "custom-name", "--clone", "owner/beta", "--via", "gh", "--branch", "dev", terminal=True)
    assert res2.exit_code == 0, res2.output
    assert cli_world.cloned[-1][:4] == ("owner/beta", str((cli_world.cwd / "beta").resolve()), "gh", "dev")
    lines = res2.stdout.splitlines()
    assert lines[0] == first_start.PROJECT_CLONED.format(
        source="owner/beta", root=str((cli_world.cwd / "beta").resolve()), tool="gh")
    assert lines[1].startswith("Registered custom-name in ")
    assert lines[-1].startswith("Next: cairnmind remember custom-name")

    # 3) Explicit NAME and ROOT with `project add --clone`
    explicit = cli_world.cwd / "nested" / "gamma-dir"
    res3 = cli_world.invoke("project", "add", "gamma", str(explicit), "--clone", "owner/gamma", terminal=False)
    assert res3.exit_code == 0, res3.output
    assert Path(json.loads(res3.stdout)["project"]["root"]) == explicit.resolve()


def test_cli_accepts_unambiguous_remote_url_without_clone_flag_but_never_shorthand_typo(cli_world):
    # Unambiguous HTTPS/SSH URL without --clone works in 1-arg and 2-arg forms
    r1 = cli_world.invoke("add", "https://github.com/owner/one", terminal=False)
    assert r1.exit_code == 0, r1.output
    assert json.loads(r1.stdout)["project"]["name"] == "one"
    assert Path(json.loads(r1.stdout)["project"]["root"]) == (cli_world.cwd / "one").resolve()

    r2 = cli_world.invoke("add", "my-two", "git@github.com:owner/two.git", terminal=False)
    assert r2.exit_code == 0, r2.output
    assert json.loads(r2.stdout)["project"]["name"] == "my-two"
    assert Path(json.loads(r2.stdout)["project"]["root"]) == (cli_world.cwd / "two").resolve()

    custom_dir = cli_world.cwd / "three-here"
    r3 = cli_world.invoke("project", "add", "https://github.com/owner/three", str(custom_dir), terminal=False)
    assert r3.exit_code == 0, r3.output
    assert json.loads(r3.stdout)["project"]["name"] == "three"
    assert Path(json.loads(r3.stdout)["project"]["root"]) == custom_dir.resolve()

    # Local path typo `src/missing` NEVER triggers clone!
    before = len(cli_world.cloned)
    typo = cli_world.invoke("project", "add", "demo", "src/missing", terminal=False)
    assert typo.exit_code == 1 and "does not exist" in str(typo.exception)
    assert len(cli_world.cloned) == before


def test_cli_clone_only_options_refused_for_local_folder_and_plain_json_unchanged(cli_world):
    assert cli_world.invoke("add").exit_code == 2
    a_file = cli_world.cwd / "not-a-dir.txt"
    a_file.write_text("x", encoding="utf-8")
    assert cli_world.invoke("add", "bad-root", str(a_file)).exit_code == 2
    folder = cli_world.cwd / "local-app"
    folder.mkdir()
    for extra in (("--via", "git"), ("--branch", "main"), ("--fresh",)):
        bad = cli_world.invoke("add", "local-app", str(folder), *extra)
        assert bad.exit_code == 2 and "only when cloning" in bad.output
    plain = cli_world.invoke("add", "local-app", str(folder), terminal=False)
    assert plain.exit_code == 0, plain.output
    assert set(json.loads(plain.stdout)) == {"project", "indexed", "history", "jobs"}


def test_cli_preflight_refuses_reader_and_conflicts_before_cloning(cli_world):
    cli_world.store._projects.append({"id": "p0", "name": "taken", "root": str(cli_world.cwd / "other")})
    taken = cli_world.invoke("add", "taken", "--clone", "owner/repo")
    assert taken.exit_code == 1 and "already registered" in str(taken.exception)
    assert cli_world.cloned == []

    cli_world.store.not_writer = "another installation writes this store"
    reader = cli_world.invoke("add", "--clone", "owner/repo")
    assert reader.exit_code == 1 and isinstance(reader.exception, StoreNotWriter)
    assert cli_world.cloned == []


def test_cli_cloned_repo_with_snapshot_stops_unless_fresh_and_published_prints_hint(cli_world):
    cli_world.memory["snapshot"] = True
    stopped = cli_world.invoke("add", "--clone", "owner/snap-app", terminal=True)
    assert stopped.exit_code == 1
    assert "Found portable project memory" in stopped.stdout and "cairnmind memory import" in stopped.stdout
    assert cli_world.store.added == []
    assert (cli_world.cwd / "snap-app" / ".memory" / "cairnmind" / "manifest.json").is_file()

    # With --fresh it registers a separate identity cleanly
    fresh = cli_world.invoke("add", "snap-fresh", str(cli_world.cwd / "snap-fresh-dir"),
                             "--clone", "owner/snap-app", "--fresh", terminal=False)
    assert fresh.exit_code == 0, fresh.output
    assert cli_world.store.added[-1] == ("snap-fresh", str((cli_world.cwd / "snap-fresh-dir").resolve()), False)

    # Published team memory: registers normally and adds hint + JSON field
    cli_world.memory["snapshot"] = False
    cli_world.memory["published"] = True
    pub = cli_world.invoke("add", "--clone", "owner/team-app", terminal=True)
    assert pub.exit_code == 0, pub.output
    assert "cairnmind memory import-published" in pub.stdout
    assert pub.stdout.splitlines()[-1].startswith("Next: cairnmind remember team-app")


# --- Broker endpoint POST /api/projects/clone -----------------------------------------------------------------------

def test_broker_clone_endpoint_preflights_and_clones(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    existing_root = tmp_path / "existing"
    existing_root.mkdir()
    existing_project = {"id": "p1", "name": "taken", "root": str(existing_root)}
    origin = "http://127.0.0.1:8766"

    def upstream(request):
        if request.url.path == "/projects":
            return httpx.Response(200, json=[existing_project])
        return httpx.Response(200, json={})

    app = create_cockpit_app(settings, runtime=SimpleNamespace(diagnostics=lambda: {}, list_runs=lambda *a: []),
                             proxy_transport=httpx.MockTransport(upstream), public_url=origin)
    client = TestClient(app)
    # Authenticate like the workspace broker client
    ticket = client.post("/_control/ticket", headers={"Authorization": f"Bearer {settings.token}", "Host": "127.0.0.1:8766"}).json()["ticket"]
    csrf = client.post("/auth/exchange", json={"ticket": ticket},
                       headers={"Origin": origin, "Host": "127.0.0.1:8766"}).json()["csrf"]
    headers = {"Origin": origin, "Host": "127.0.0.1:8766", "x-csrf-token": csrf}

    parent = tmp_path / "workspace"
    parent.mkdir()
    called = []

    def fake_clone(source, destination, *, via="auto", branch=None, interactive=False, **kw):
        assert interactive is False
        called.append((source.raw, str(destination), via, branch))
        destination.mkdir(parents=True, exist_ok=True)
        (destination / ".memory" / "cairnmind-published").mkdir(parents=True, exist_ok=True)
        return {"source": source.raw, "url": source.git_url, "tool": "git", "destination": str(destination)}

    monkeypatch.setattr(project_clone, "choose_tool", lambda source, via="auto", **kw: "git")
    monkeypatch.setattr(project_clone, "clone_repository", fake_clone)

    # Conflict with active project refuses BEFORE cloning
    conflict = client.post("/api/projects/clone", json={"repo": "owner/taken", "parent": str(parent)}, headers=headers)
    assert conflict.status_code == 409 and called == []

    # Invalid URL refused
    bad = client.post("/api/projects/clone", json={"repo": "http://github.com/o/r", "parent": str(parent)}, headers=headers)
    assert bad.status_code == 422 and called == []

    # Happy path
    ok = client.post("/api/projects/clone",
                     json={"repo": "owner/newrepo", "parent": str(parent), "via": "git", "branch": "main"},
                     headers=headers)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["name"] == "newrepo" and Path(body["root"]) == (parent / "newrepo").resolve()
    assert body["published_memory"] and body["memory_snapshot"] is None
    assert called == [("owner/newrepo", str((parent / "newrepo").resolve()), "git", "main")]


# --- TUI: Clone repository modal and command palette ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_tui_clone_repository_form_validates_clones_and_hands_off_to_register(tmp_path, monkeypatch):
    settings, broker, workspace = setup_panel(tmp_path, monkeypatch)
    parent = tmp_path / "src"
    parent.mkdir()
    cloned_root = str(parent / "demo")

    orig_request = broker.request

    def broker_request(method, path, *, params=None, json=None, timeout=20):
        if path == "/api/projects/clone":
            broker.calls.append((method, path, params or {}, dict(json or {})))
            return {"cloned": {"source": json["repo"], "tool": "gh", "destination": cloned_root},
                    "root": cloned_root, "name": json.get("name") or "demo",
                    "memory_snapshot": None, "published_memory": cloned_root + "/.memory/cairnmind-published"}
        return orig_request(method, path, params=params, json=json, timeout=timeout)

    broker.request = broker_request
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(100, 42)) as pilot:
        await settle(app, pilot)
        app.clone_project()
        await pilot.pause()
        # Inline validation rejects invalid tool before calling broker
        app.screen.query_one("#field-repo", Input).value = "owner/demo"
        app.screen.query_one("#field-parent", Input).value = str(parent)
        app.screen.query_one("#field-via", Input).value = "svn"
        await form_ready(app, pilot)
        await pilot.click("#form-submit")
        await form_ready(app, pilot)
        assert not any(path == "/api/projects/clone" for _, path, _, _ in broker.calls)
        assert "auto, gh" in str(app.screen.query_one("#form-error", Static).render())

        # Valid submission clones and then registers the project
        app.screen.query_one("#field-via", Input).value = "auto"
        await pilot.press("ctrl+s")
        await settle(app, pilot)

    clone_calls = [body for _, path, _, body in broker.calls if path == "/api/projects/clone"]
    assert clone_calls == [{"repo": "owner/demo", "parent": str(parent), "via": "auto"}]
    assert any(path == "/api/projects/inspect" and body["root"] == cloned_root for _, path, _, body in broker.calls)
    assert any(path == "/api/projects" and body["root"] == cloned_root for _, path, _, body in broker.calls)


@pytest.mark.asyncio
async def test_tui_clone_repository_hands_snapshot_to_existing_import_choice(tmp_path, monkeypatch):
    from cairnmind.terminal.terminal_ui import ChoiceModal
    settings, broker, workspace = setup_panel(tmp_path, monkeypatch)
    parent = tmp_path / "src"
    parent.mkdir()
    cloned_root = str(parent / "carried")
    broker.inspect_memory = cloned_root + "/.memory/cairnmind"
    orig_request = broker.request

    def broker_request(method, path, *, params=None, json=None, timeout=20):
        if path == "/api/projects/clone":
            broker.calls.append((method, path, params or {}, dict(json or {})))
            return {"cloned": {"source": json["repo"], "tool": "git", "destination": cloned_root},
                    "root": cloned_root, "name": "carried",
                    "memory_snapshot": broker.inspect_memory, "published_memory": None}
        return orig_request(method, path, params=params, json=json, timeout=timeout)

    broker.request = broker_request
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(100, 42)) as pilot:
        await settle(app, pilot)
        app.clone_project()
        await pilot.pause()
        app.screen.query_one("#field-repo", Input).value = "owner/carried"
        app.screen.query_one("#field-parent", Input).value = str(parent)
        await form_ready(app, pilot)
        await pilot.press("ctrl+s")
        for _ in range(50):
            await pilot.pause(0.02)
            if isinstance(app.screen, ChoiceModal):
                break
        assert isinstance(app.screen, ChoiceModal)
        assert [value for _, value in app.screen.choices] == ["import", "new"]
        await pilot.press("escape")
        await settle(app, pilot)
    assert not any(path == "/api/projects" for _, path, _, _ in broker.calls)


@pytest.mark.asyncio
async def test_command_palette_finds_clone_repository_and_dispatches(tmp_path):
    calls = []
    manager = SimpleNamespace(settings=Settings(data_dir=tmp_path / "data"),
                              request_command=lambda value: calls.append(value))
    command = next(row for row in COMMANDS if row[3] == "clone_project")
    dispatch_command(manager, command)
    assert calls == ["clone_project"]
    app = CommandPaletteApp(manager)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press(*"clone")
        await pilot.pause()
        assert len(app.rows) == 1 and app.rows[0][3] == "clone_project"
        await pilot.press("enter")
    assert app.return_value[3] == "clone_project"


# --- Polish i18n completeness ---------------------------------------------------------------------------------------

def test_all_new_clone_messages_have_polish_with_matching_placeholders():
    placeholders = re.compile(r"\{[a-z_]+\}")
    for english, polish in project_clone.MESSAGES.items():
        assert polish != english, english
        assert set(placeholders.findall(english)) == set(placeholders.findall(polish)), english
        assert tr(english, "pl") == polish
    for name in ("PROJECT_CLONED", "CLONE_SNAPSHOT_FOUND", "CLONE_SNAPSHOT_NEXT", "CLONE_PUBLISHED_FOUND"):
        english = getattr(first_start, name)
        polish = first_start.MESSAGES[english]
        assert polish != english and set(placeholders.findall(english)) == set(placeholders.findall(polish)), name
    for ui_text in ("Clone repository", "Clone from GitHub or Git and register",
                    "Repository (owner/repo, HTTPS or SSH URL)", "Parent folder (Windows or WSL)",
                    "Name (optional; defaults to repository name)", "Tool: auto, gh or git",
                    "Branch or tag (optional)", "Cloning repository…"):
        assert tr(ui_text, "pl") != ui_text, ui_text


def test_clone_edge_cases_parser_auth_preflight_interactive_and_errors(tmp_path, monkeypatch):
    import io
    # Non-string & malformed URL/branch edges
    assert project_clone.is_explicit_remote_url("https://github.com/o/r bad") is False
    for bad in (123, "https://github.com/onlyone", "https://github.com/owner/.git", "https://-bad/owner/repo",
                "ssh://-bad@github.com/owner/repo.git", "ssh://git@github.com:notaport/owner/repo.git",
                "git@github.com:/owner/repo.git", "git@github.com:owner\\repo.git"):
        with pytest.raises(ValueError):
            project_clone.parse_clone_source(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        project_clone.validate_branch(None)  # type: ignore[arg-type]

    # gh_authenticated branches (0, 1, OSError, TimeoutExpired)
    assert project_clone.gh_authenticated("github.com", runner=lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", "")) is True
    assert project_clone.gh_authenticated("github.com", runner=lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "")) is False
    assert project_clone.gh_authenticated("github.com", runner=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())) is False
    assert project_clone.gh_authenticated("github.com", runner=lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("gh", 5))) is False

    # check_store_preflight branches (empty name, >256, root taken, project_roots query conflict)
    existing_root = tmp_path / "already"
    existing_root.mkdir()
    extra_root = tmp_path / "extra"
    extra_root.mkdir()

    class QueryStore(FakeStore):
        def require_safe_project_root(self, root):
            return str(Path(root).expanduser().resolve(strict=False))

        def query(self, sql, params=None):
            return [{"project_id": "p1"}] if params and str(extra_root.resolve()) in params.values() else []

    qstore = QueryStore(tmp_path / "data", [{"id": "p1", "name": "one", "root": str(existing_root)}])
    with pytest.raises(ValueError, match="must not be empty"):
        project_clone.check_store_preflight(qstore, "   ", tmp_path / "new")
    with pytest.raises(ValueError, match="at most 256"):
        project_clone.check_store_preflight(qstore, "n" * 257, tmp_path / "new")
    with pytest.raises(ValueError, match="already registered"):
        project_clone.check_store_preflight(qstore, "two", existing_root)
    with pytest.raises(ValueError, match="already registered"):
        project_clone.check_store_preflight(qstore, "two", extra_root)

    # Interactive cloning (StringIO fallback + real fd stream + failure above) & subprocess error branches
    src = project_clone.parse_clone_source("owner/demo")
    buf = io.StringIO()
    ok = project_clone.clone_repository(
        src, tmp_path / "i1", via="git", interactive=True, status_out=buf,
        which=lambda n: f"/usr/bin/{n}", runner=lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", ""))
    assert ok["tool"] == "git" and "Cloning owner/demo" in buf.getvalue()

    with (
        (tmp_path / "stream.log").open("w+", encoding="utf-8") as handle,
        pytest.raises(project_clone.CloneError, match="its output is above"),
    ):
        project_clone.clone_repository(
            src, tmp_path / "i2", via="gh", interactive=True, status_out=handle,
            which=lambda n: f"/usr/bin/{n}", runner=lambda argv, **k: subprocess.CompletedProcess(argv, 1, None, None))

    # Auth hint when gh is NOT installed
    with pytest.raises(project_clone.CloneError, match="install and sign in to `gh`"):
        project_clone.clone_repository(
            src, tmp_path / "i3", via="git", which=lambda n: "/usr/bin/git" if n == "git" else None,
            runner=lambda argv, **k: subprocess.CompletedProcess(argv, 128, "", "fatal: Repository not found\n"))

    # FileNotFoundError, TimeoutExpired, OSError from runner
    with pytest.raises(project_clone.CloneError, match="git was not found"):
        project_clone.clone_repository(
            src, tmp_path / "i4", via="git", which=lambda n: "/usr/bin/git",
            runner=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(project_clone.CloneError, match="did not finish within"):
        project_clone.clone_repository(
            src, tmp_path / "i5", via="git", timeout=12, which=lambda n: "/usr/bin/git",
            runner=lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", 12)))
    with pytest.raises(project_clone.CloneError, match="Permission denied"):
        project_clone.clone_repository(
            src, tmp_path / "i6", via="git", which=lambda n: "/usr/bin/git",
            runner=lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))


def test_broker_clone_endpoint_destination_and_error_edges(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    origin = "http://127.0.0.1:8766"
    app = create_cockpit_app(
        settings, runtime=SimpleNamespace(diagnostics=lambda: {}, list_runs=lambda *a: []),
        proxy_transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[])), public_url=origin)
    client = TestClient(app)
    ticket = client.post("/_control/ticket", headers={"Authorization": f"Bearer {settings.token}", "Host": "127.0.0.1:8766"}).json()["ticket"]
    csrf = client.post("/auth/exchange", json={"ticket": ticket}, headers={"Origin": origin, "Host": "127.0.0.1:8766"}).json()["csrf"]
    headers = {"Origin": origin, "Host": "127.0.0.1:8766", "x-csrf-token": csrf}

    # Missing parent/destination, non-existent parent, invalid param types
    assert client.post("/api/projects/clone", json={"repo": "owner/r"}, headers=headers).status_code == 422
    assert client.post("/api/projects/clone", json={"repo": "owner/r", "parent": str(tmp_path / "nope")}, headers=headers).status_code == 422
    assert client.post("/api/projects/clone", json={"repo": 123, "parent": str(tmp_path)}, headers=headers).status_code == 422
    assert client.post("/api/projects/clone", json={"repo": "owner/r", "via": 1, "parent": str(tmp_path)}, headers=headers).status_code == 422

    # Explicit destination + clone failure propagated as 422
    dest = tmp_path / "custom-dest"
    monkeypatch.setattr(project_clone, "choose_tool", lambda source, via="auto", **kw: "git")
    monkeypatch.setattr(project_clone, "clone_repository", lambda *a, **k: (_ for _ in ()).throw(project_clone.CloneError("network down")))
    failed = client.post("/api/projects/clone", json={"repo": "owner/r", "destination": str(dest)}, headers=headers)
    assert failed.status_code == 422 and "network down" in failed.json()["detail"]
