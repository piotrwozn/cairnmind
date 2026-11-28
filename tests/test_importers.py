"""External transcript converter commands and degraded history import (#127)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.agents.retrieval import Retriever
from cairnmind.config import Settings
from cairnmind.memory import importers
from cairnmind.memory.evidence import evidence_states
from cairnmind.memory.sessions import SessionManager
from cairnmind.store import Store
from cairnmind.terminal.inspector import Inspector

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clear_session_env(monkeypatch):
    for key in (
        "CAIRNMIND_SESSION_ID",
        "CAIRNMIND_PACKET_ID",
        "DEVBRAIN_SESSION_ID",
        "DEVBRAIN_PACKET_ID",
    ):
        monkeypatch.delenv(key, raising=False)


def _write_converter_script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _converter_script() -> str:
    return (
        "import json, sys\n"
        "from pathlib import Path\n"
        "src = Path(sys.argv[1]).read_text(encoding='utf-8')\n"
        "for idx, line in enumerate(src.splitlines(), start=1):\n"
        "    role, cwd, text = line.split('|', 2)\n"
        "    print(json.dumps({\n"
        "        'version': 1,\n"
        "        'role': role,\n"
        "        'content': text,\n"
        "        'session_id': 'acme-sess-1',\n"
        "        'timestamp': f'2026-11-26T10:00:0{idx}Z',\n"
        "        'cwd': cwd,\n"
        "        'event_id': f'ev-{idx}',\n"
        "    }), flush=True)\n"
    )


def test_registry_crud_validation_and_session_refusals(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    script = _write_converter_script(tmp_path / "conv.py", _converter_script())

    assert importers.list_importers(data_dir) == []
    # Human/non-JSON paths on CLI: empty list, add, remove.
    empty_list = runner.invoke(cli.app, ["--data-dir", str(data_dir), "importer", "list"])
    assert empty_list.exit_code == 0 and json.loads(empty_list.output)["importers"] == []

    # Invalid names, reserved names, relative commands, bounds
    with pytest.raises(ValueError, match="lowercase letters"):
        importers.validate_importer("BadName", [sys.executable])
    with pytest.raises(ValueError, match="reserved"):
        importers.validate_importer("codex", [sys.executable])
    with pytest.raises(ValueError, match="absolute executable path"):
        importers.validate_importer("acme", ["python3", str(script)])
    with pytest.raises(ValueError, match="absolute executable path"):
        importers.validate_importer("acme", ["/usr/bin/../bin/python3"])
    with pytest.raises(ValueError, match="timeout_seconds"):
        importers.validate_importer("acme", [sys.executable], timeout_seconds=0)
    with pytest.raises(ValueError, match="output_limit_bytes"):
        importers.validate_importer("acme", [sys.executable], output_limit_bytes=0)
    with pytest.raises(ValueError, match="1 to 32 nonempty strings"):
        importers.validate_importer("acme", [sys.executable, "\x01bad"])

    # Registry corruption / wrong version / MAX_IMPORTERS guard
    reg_file = data_dir / importers.IMPORTERS_FILENAME
    data_dir.mkdir(parents=True, exist_ok=True)
    reg_file.write_text(json.dumps({"version": 99, "importers": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid importer registry format"):
        importers.list_importers(data_dir)
    reg_file.write_text(json.dumps({"version": 1, "importers": "bad"}), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid importer registry entries"):
        importers.list_importers(data_dir)
    reg_file.unlink()

    # Terminal-mode sentences callback exercised directly
    from unittest.mock import patch
    with patch.object(cli, "_stdout_is_terminal", return_value=True):
        t_empty = runner.invoke(cli.app, ["--data-dir", str(data_dir), "importer", "list"])
        assert "No external transcript importers registered" in t_empty.output
        t_add = runner.invoke(
            cli.app, ["--data-dir", str(data_dir), "importer", "add", "term-imp", sys.executable, str(script)]
        )
        assert "Registered importer 'term-imp'" in t_add.output
        t_list = runner.invoke(cli.app, ["--data-dir", str(data_dir), "importer", "list"])
        assert "term-imp:" in t_list.output
        t_rm = runner.invoke(cli.app, ["--data-dir", str(data_dir), "importer", "remove", "term-imp"])
        assert "Removed importer 'term-imp'" in t_rm.output

    # CLI add / list / remove happy path (JSON and terminal modes)
    added = runner.invoke(
        cli.app,
        [
            "--data-dir", str(data_dir),
            "importer", "add", "acme", sys.executable, str(script),
            "--timeout", "30", "--output-limit", "65536", "--json",
        ],
    )
    assert added.exit_code == 0, added.output
    entry = json.loads(added.output)
    assert entry["name"] == "acme"
    assert entry["argv"] == [sys.executable, str(script)]
    assert entry["timeout_seconds"] == 30
    assert entry["output_limit_bytes"] == 65536

    # Duplicate without --replace fails
    dup = runner.invoke(
        cli.app,
        ["--data-dir", str(data_dir), "importer", "add", "acme", sys.executable, str(script)],
    )
    assert dup.exit_code == 2

    # Duplicate with --replace succeeds
    rep = runner.invoke(
        cli.app,
        ["--data-dir", str(data_dir), "importer", "add", "acme", sys.executable, str(script), "--replace"],
    )
    assert rep.exit_code == 0, rep.output

    listed = runner.invoke(cli.app, ["--data-dir", str(data_dir), "importer", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    assert [row["name"] for row in json.loads(listed.output)["importers"]] == ["acme"]

    # Mutating/listing commands refuse inside a coding session
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "sess-123")
    for argv in (
        ["--data-dir", str(data_dir), "importer", "add", "other", sys.executable],
        ["--data-dir", str(data_dir), "importer", "list"],
        ["--data-dir", str(data_dir), "importer", "remove", "acme"],
        ["--data-dir", str(data_dir), "import", str(script), "--via", "acme"],
    ):
        refused = runner.invoke(cli.app, argv)
        assert refused.exit_code == 2, (argv, refused.output)
        assert "outside a coding session" in refused.output
    monkeypatch.delenv("CAIRNMIND_SESSION_ID")

    removed = runner.invoke(cli.app, ["--data-dir", str(data_dir), "importer", "remove", "acme", "--json"])
    assert removed.exit_code == 0, removed.output
    assert json.loads(removed.output)["removed"] is True
    assert importers.list_importers(data_dir) == []

    missing_rm = runner.invoke(cli.app, ["--data-dir", str(data_dir), "importer", "remove", "acme"])
    assert missing_rm.exit_code == 2


def test_parse_converted_jsonl_strict_validation(tmp_path):
    line = json.dumps({"version": 1, "role": "user", "content": "Hello\nworld", "session_id": "s1"})
    parsed = importers.parse_converted_jsonl((line + "\n").encode("utf-8"), default_session_id="fallback")
    assert len(parsed) == 1
    assert parsed[0]["session_id"] == "s1"

    assert importers.parse_converted_jsonl(b"", default_session_id="fallback") == []

    bad_payloads = [
        (b'{"version":1,"role":"user","content":"unterminated"}', "trailing newline"),
        (b"\xff\xfe\n", "not valid UTF-8"),
        (b"\n", "blank"),
        (b"not-json\n", "not valid JSON"),
        (b"[1, 2]\n", "must be a JSON object"),
        (b'{"version":1,"role":"user","content":"hi","extra":1}\n', "unsupported fields"),
        (b'{"version":2,"role":"user","content":"hi"}\n', "version 1"),
        (b'{"version":1,"role":"admin","content":"hi"}\n', "invalid role"),
        (b'{"version":1,"role":"user","content":"   "}\n', "nonempty UTF-8"),
        (b'{"version":1,"role":"user","content":"ok","session_id":"  "}\n', "invalid session_id"),
        (b'{"version":1,"role":"user","content":"ok","timestamp":123}\n', "invalid timestamp"),
    ]
    for raw, match in bad_payloads:
        with pytest.raises(ValueError, match=match):
            importers.parse_converted_jsonl(raw, default_session_id="fallback")

    over_line = (json.dumps({"version": 1, "role": "user", "content": "x" * (importers.MAX_LINE_BYTES + 10)}) + "\n").encode("utf-8")
    with pytest.raises(ValueError, match="exceeds"):
        importers.parse_converted_jsonl(over_line, default_session_id="fallback")

    # Missing executable inside execute_converter raises ImporterExecutionError
    with pytest.raises(importers.ImporterExecutionError, match="Failed to start importer"):
        importers.execute_converter(
            {"name": "missing-bin", "argv": [str(Path(sys.executable).parent / "does-not-exist-cairnmind-bin")],
             "timeout_seconds": 5, "output_limit_bytes": 1024},
            Path(sys.executable),
        )

    # Extra registry & parser edge cases
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    reg_file = data_dir / importers.IMPORTERS_FILENAME
    reg_file.write_text(json.dumps({"version": 1, "importers": {"bad": "not-a-dict"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid importer entry"):
        importers.list_importers(data_dir)
    many = {f"imp-{idx}": {"name": f"imp-{idx}", "argv": [sys.executable], "timeout_seconds": 10,
                           "output_limit_bytes": 1024} for idx in range(importers.MAX_IMPORTERS)}
    reg_file.write_text(json.dumps({"version": 1, "importers": many}), encoding="utf-8")
    with pytest.raises(ValueError, match="At most"):
        importers.add_importer(data_dir, "extra-one", [sys.executable])
    reg_file.unlink()

    with pytest.raises(ValueError, match="invalid cwd"):
        importers.parse_converted_jsonl(
            (json.dumps({"version": 1, "role": "user", "content": "hi", "cwd": ""}) + "\n").encode("utf-8"),
            default_session_id="d",
        )
    with pytest.raises(ValueError, match="Unknown importer"):
        importers.get_importer(data_dir, "absent")


def test_execute_converter_cleans_env_and_enforces_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRNMIND_TOKEN", "super-secret-token")
    monkeypatch.setenv("DEVBRAIN_ARCADE_PASSWORD", "secret-pw")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")

    env_script = _write_converter_script(
        tmp_path / "env_dump.py",
        "import json, os\n"
        "leaked = [k for k in os.environ if k.startswith(('CAIRNMIND_', 'DEVBRAIN_')) or k == 'OPENAI_API_KEY']\n"
        "assert not leaked, f'leaked: {leaked}'\n"
        "print(json.dumps({'version': 1, 'role': 'assistant', 'content': 'clean env ok'}), flush=True)\n",
    )
    out = importers.execute_converter(
        {"name": "envcheck", "argv": [sys.executable, str(env_script)], "timeout_seconds": 10,
         "output_limit_bytes": 65536},
        tmp_path / "dummy.txt",
    )
    rows = importers.parse_converted_jsonl(out, default_session_id="d")
    assert rows[0]["content"] == "clean env ok"

    fail_script = _write_converter_script(
        tmp_path / "fail.py",
        "import sys\nsys.stderr.write('converter boom')\nsys.exit(3)\n",
    )
    with pytest.raises(importers.ImporterExecutionError, match="exited with code 3: converter boom"):
        importers.execute_converter(
            {"name": "fail", "argv": [sys.executable, str(fail_script)], "timeout_seconds": 10,
             "output_limit_bytes": 65536},
            tmp_path / "dummy.txt",
        )

    flood_script = _write_converter_script(
        tmp_path / "flood.py",
        "import sys\nfor _ in range(20):\n    sys.stdout.write('x' * 512 + '\\n')\n    sys.stdout.flush()\n",
    )
    with pytest.raises(importers.ImporterExecutionError, match="output limit"):
        importers.execute_converter(
            {"name": "flood", "argv": [sys.executable, str(flood_script)], "timeout_seconds": 10,
             "output_limit_bytes": 256},
            tmp_path / "dummy.txt",
        )

    slow_script = _write_converter_script(
        tmp_path / "slow.py",
        "import time\ntime.sleep(5)\n",
    )
    with pytest.raises(importers.ImporterExecutionError, match="timeout"):
        importers.execute_converter(
            {"name": "slow", "argv": [sys.executable, str(slow_script)], "timeout_seconds": 1,
             "output_limit_bytes": 65536},
            tmp_path / "dummy.txt",
        )


@pytest.mark.arcade
def test_acceptance_converter_end_to_end_marks_degraded_preserves_native_resume_and_masks_secrets(tmp_path):
    import subprocess

    data_dir = tmp_path / "data"
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "app.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    store = Store(data_dir)
    project = store.add_project("acme-proj", repo)

    # Establish a verified native session resume packet before importing third-party history.
    settings = Settings.load(data_dir)
    sessions = SessionManager(store, settings)
    baseline_packet = sessions.prepare(project["id"], "codex", repo, reason="startup")
    assert baseline_packet["packet"]["status"] == "ready"
    assert baseline_packet["packet"]["archive"]["status"] == "complete"
    assert "history" not in baseline_packet["packet"]

    script = _write_converter_script(tmp_path / "acme_conv.py", _converter_script())
    add_res = runner.invoke(
        cli.app,
        ["--data-dir", str(data_dir), "importer", "add", "acme", sys.executable, str(script), "--json"],
    )
    assert add_res.exit_code == 0, add_res.output

    foreign_dir = tmp_path / "foreign"
    foreign_dir.mkdir()
    transcript = foreign_dir / "session.acme"
    fake_gh = "ghp_" + ("1234567890abcdef" * 2) + "1234"
    transcript.write_text(
        f"user|{repo}|Acme decision: use token {fake_gh} for webhook auth\n"
        f"assistant|{repo}|Stored decision for project root {repo}\n",
        encoding="utf-8",
    )

    imported = runner.invoke(
        cli.app,
        [
            "--data-dir", str(data_dir),
            "import", str(transcript), "--via", "acme", "--project", "acme-proj",
        ],
    )
    assert imported.exit_code == 0, imported.output
    summary = json.loads(imported.output)
    assert summary["importer"] == "acme"
    assert summary["archived"] == 2
    assert summary["chunks"] >= 2
    assert summary["inbox"] == 0
    assert summary["evidence_state"] == "degraded"

    # Mutually exclusive flags and internal data_dir files are refused.
    bad_flags = runner.invoke(
        cli.app,
        ["--data-dir", str(data_dir), "import", str(transcript), "--via", "acme", "--client", "codex"],
    )
    assert bad_flags.exit_code == 2

    inside_data = data_dir / "leak.acme"
    inside_data.write_text("user|hello\n", encoding="utf-8")
    refused_inside = importers.run_converter_import
    with pytest.raises(importers.ImporterRefused, match="inside Cairnmind"):
        refused_inside(store, settings, inside_data, "acme", "acme-proj")

    # Converted history creates no import_cursors and no extract jobs; search finds the masked text.
    assert store.query("SELECT FROM import_cursors") == []
    assert store.query("SELECT FROM jobs WHERE kind='extract'") == []

    hits = Retriever(store, settings).search(project["id"], "webhook auth")
    assert hits and any(h["source_id"] in summary["source_ids"] for h in hits)
    stored_sources = [store.get_source(sid) for sid in summary["source_ids"]]
    assert all(src["kind"] == "converted_transcript" for src in stored_sources)
    assert all(src["metadata"]["importer"] == "acme" for src in stored_sources)
    assert all(src["metadata"]["origin_evidence"] == "degraded" for src in stored_sources)
    assert all(src["metadata"]["evidence_verified"] is False for src in stored_sources)
    # Registered/well-known secret is masked in searchable normalized_text.
    assert "[REDACTED]" in stored_sources[0]["metadata"]["normalized_text"]
    assert fake_gh not in stored_sources[0]["metadata"]["normalized_text"]

    # Record citing only converted sources stays degraded (never anchored).
    rec = store.remember(
        project["id"],
        {"kind": "decision", "content": "Webhook token rule", "key": "webhook-rule"},
        [summary["source_ids"][0]],
    )
    ev = evidence_states(store, project["id"], [rec])[rec["id"]]
    assert ev["evidence_state"] == "degraded"

    # Inspector graph groups converted_transcript sources under their session bucket.
    graph = Inspector(store).graph(project["id"])
    assert any(
        n.get("kind") == "message"
        and n.get("client") == "importer:acme"
        and n.get("session_id") == "acme-sess-1"
        for n in graph["nodes"]
    )

    # Native resume readiness stays intact, unaffected by converted transcript.
    after_packet = sessions.prepare(project["id"], "codex", repo, reason="refresh")
    assert after_packet["packet"]["status"] == "ready"
    assert after_packet["packet"]["archive"]["status"] == "complete"
    assert "history" not in after_packet["packet"]

    # Removing the importer keeps the stored sources and their degraded mark intact.
    removed = runner.invoke(cli.app, ["--data-dir", str(data_dir), "importer", "remove", "acme", "--json"])
    assert removed.exit_code == 0, removed.output
    assert importers.list_importers(data_dir) == []
    kept = store.get_source(summary["source_ids"][0])
    assert kept["metadata"]["importer"] == "acme"
    assert kept["metadata"]["origin_evidence"] == "degraded"

    # Unmatched cwd goes to ingestion_inbox when --project is omitted.
    re_added = runner.invoke(
        cli.app,
        ["--data-dir", str(data_dir), "importer", "add", "acme2", sys.executable, str(script)],
    )
    assert re_added.exit_code == 0
    unmatched = foreign_dir / "unmatched.acme"
    unmatched.write_text(
        f"user|{foreign_dir}|Unmatched first line\n"
        f"assistant|{foreign_dir}|Unmatched second line\n",
        encoding="utf-8",
    )
    inbox_res = runner.invoke(
        cli.app,
        ["--data-dir", str(data_dir), "import", str(unmatched), "--via", "acme2"],
    )
    assert inbox_res.exit_code == 0, inbox_res.output
    inbox_data = json.loads(inbox_res.output)
    assert inbox_data["inbox"] == 2
    assert len(store.query("SELECT FROM ingestion_inbox WHERE client='importer:acme2'")) == 2


def test_importer_edge_errors_and_sigkill_escalation(tmp_path):
    """Cover non-UTF8 output, symlinked registry, missing executable, and SIGTERM-ignoring converters."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="nonempty strings"):
        importers.validate_importer("bad-surrogate", ["/usr/bin/python3", "\ud800"])

    with pytest.raises(ValueError, match="raw UTF-8"):
        importers.parse_converted_jsonl("not-bytes", default_session_id="bin")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="valid UTF-8"):
        importers.parse_converted_jsonl(b"\xff\xfe\n", default_session_id="bin")

    # Corrupted importers.json produces a structured CLI refusal on `importer list`.
    reg_path = data_dir / importers.IMPORTERS_FILENAME
    reg_path.write_text("{not-json", encoding="utf-8")
    corrupt_res = runner.invoke(cli.app, ["--data-dir", str(data_dir), "importer", "list"])
    assert corrupt_res.exit_code != 0
    reg_path.unlink()

    # Symlinked importers.json must be refused when reading.
    real_target = tmp_path / "real.json"
    real_target.write_text('{"version": 1, "importers": {}}', encoding="utf-8")
    try:
        reg_path.symlink_to(real_target)
    except OSError:
        pass
    else:
        with pytest.raises(ValueError, match="symbolic link"):
            importers.list_importers(data_dir)
        reg_path.unlink()

    # Nonexistent executable raises ImporterExecutionError.
    missing_exe = str((tmp_path / "missing-converter-exe").resolve())
    with pytest.raises(importers.ImporterExecutionError, match="Failed to start importer"):
        importers.execute_converter(
            {"name": "ghost", "argv": [missing_exe], "timeout_seconds": 2, "output_limit_bytes": 1024},
            tmp_path / "ignored.txt",
        )

    # Converter that traps SIGTERM and ignores it is escalated to SIGKILL on timeout.
    stubborn = tmp_path / "stubborn.py"
    stubborn.write_text(
        "import signal, time\n"
        "if hasattr(signal, 'SIGTERM'):\n"
        "    signal.signal(signal.SIGTERM, lambda *_: None)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    with pytest.raises(importers.ImporterExecutionError, match="exceeded its timeout"):
        importers.execute_converter(
            {
                "name": "stubborn",
                "argv": [sys.executable, str(stubborn)],
                "timeout_seconds": 1,
                "output_limit_bytes": 1024,
            },
            tmp_path / "ignored.txt",
        )


@pytest.mark.arcade
def test_run_converter_import_missing_file_and_invalid_masker(tmp_path):
    """Cover missing history file and secret-config refusal before spawning any converter."""
    data_dir = tmp_path / "data"
    settings = Settings.load(data_dir, create=True)
    store = Store(data_dir, settings=settings)

    conv = tmp_path / "conv.py"
    conv.write_text("print('{}')\n", encoding="utf-8")
    importers.add_importer(data_dir, "edge", [sys.executable, str(conv)])

    with pytest.raises(ValueError, match="History does not exist"):
        importers.run_converter_import(store, settings, tmp_path / "nope.log", "edge")

    # Broken cockpit preferences prevent reading saved secrets and refuse before running converter.
    cockpit_prefs = data_dir / "cockpit" / "preferences.json"
    cockpit_prefs.parent.mkdir(parents=True, exist_ok=True)
    cockpit_prefs.write_text("{corrupt", encoding="utf-8")
    real_input = tmp_path / "real.log"
    real_input.write_text("hello\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Transcript import refused"):
        importers.run_converter_import(store, settings, real_input, "edge")
