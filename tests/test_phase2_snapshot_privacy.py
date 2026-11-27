"""Phase 2 tests: snapshot privacy profiles, quote-by-reference, fail-closed secrets/redaction,
.cairnmindignore + git check-ignore, rebase stale_ref handling, snapshot merge, memory audit and pre-commit.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.ignore_rules import IgnoreMatcher, escapes_root, git_check_ignore, is_binary_bytes
from cairnmind.ingest import _files
from cairnmind.integrations import PRE_COMMIT_HOOK_ID, install_pre_commit_hook
from cairnmind.memory import MemoryConflict, inspect_snapshot
from cairnmind.memory_audit import audit_memory
from cairnmind.memory_published_secrets import (
    REGISTERED, Rules, SecretRulesError, load_rules, random_looking, redact_text, scan_text, value_sha256,
)
from cairnmind.service import MemoryPublishBody
from cairnmind.snapshot_privacy import SnapshotPrivacyError, apply_snapshot_privacy, merge_snapshots, resolve_snapshot_refs

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True
    ).stdout.decode("utf-8", "replace").strip()


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Phase 2 Fixture")
    _git(root, "config", "user.email", "phase2@example.invalid")
    (root / "app.py").write_text("VALUE = 1\nLINE2 = 2\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.log\n*.env\n!important.env\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    return root


def _sample_pem() -> str:
    return (
        "-----BEGIN " + "RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy0AHB7M\n"
        "ABCD1234EFGH5678IJKL9012MNOP3456QRST7890UVWX1234\n"
        "-----END RSA " + "PRIVATE KEY-----"
    )


def _sample_jwt() -> str:
    return "eyJ" + "hbGciOiJIUzI1NiJ9.eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"


def _sample_aws() -> str:
    return "AKIA" + "IOSFODNN7EXAMPLE"


def _make_payload(project_id: str, sources: list[dict], records: list[dict] | None = None) -> dict:
    records = records or []
    nodes = [
        {"id": f"source:{s['id']}", "project_id": project_id, "kind": "source",
         "label": s["uri"], "source_id": s["id"], "metadata": {"uri": s["uri"], "kind": s["kind"]}}
        for s in sources
    ]
    edges = []
    record_revisions = []
    for rec in records:
        nodes.append({
            "id": f"record:{rec['id']}", "project_id": project_id, "kind": rec["kind"],
            "label": rec["content"][:1000], "source_id": rec["source_ids"][0],
            "metadata": {**rec["metadata"], "record_id": rec["id"], "status": rec["status"]},
        })
        for sid in rec["source_ids"]:
            edges.append({
                "id": hashlib.sha256(f"{rec['id']}:{sid}".encode()).hexdigest(),
                "project_id": project_id, "from_id": f"record:{rec['id']}",
                "to_id": f"source:{sid}", "kind": "supported_by", "source_id": sid, "status": rec["status"],
            })
        record_revisions.append({
            "id": f"{rec['id']}:{rec['version']}", "project_id": project_id,
            "record_id": rec["id"], "version": rec["version"], "snapshot": dict(rec),
        })
    return {
        "sources": sources, "records": records, "record_revisions": record_revisions,
        "plans": [], "plan_revisions": [], "nodes": nodes, "edges": edges, "check_runs": [],
    }


def _source_row(project_id: str, uri: str, content: str, kind: str, metadata: dict | None = None) -> dict:
    return {
        "id": str(uuid4()), "project_id": project_id, "uri": uri, "kind": kind,
        "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "metadata": metadata or {}, "is_current": True, "created_at": "2026-11-27T00:00:00+00:00",
    }


def _record_row(project_id: str, content: str, source_ids: list[str], *, key: str = "k1", version: int = 1) -> dict:
    return {
        "id": str(uuid4()), "project_id": project_id, "kind": "decision", "key": key,
        "content": content, "status": "proposed", "source_ids": source_ids,
        "version": version, "supersedes": None, "metadata": {},
        "created_at": "2026-11-27T00:00:00+00:00", "updated_at": "2026-11-27T00:00:00+00:00",
    }


def _write_package(root: Path, project_id: str, payload: dict, *,
                   profile: str = "team", contains_transcripts: bool = False,
                   redacted: bool = False, parent_id: str | None = None,
                   ancestors: list[str] | None = None,
                   subdir: Path | None = None) -> Path:
    from cairnmind.memory import FORMAT, TABLES, VERSION, _bytes, _descriptors, _digest, _manifest_id, _state_hash
    out = (subdir or (root / ".memory" / "cairnmind"))
    (out / "objects").mkdir(parents=True, exist_ok=True)
    (out / "manifests").mkdir(parents=True, exist_ok=True)
    objects = _descriptors(payload)
    for table, rows in payload.items():
        for desc, row in zip(objects[table], rows, strict=True):
            (out / desc["path"]).write_bytes(_bytes(row))
    captured = {
        "project": {"id": project_id, "name": "phase2-demo"},
        "origin_root": str(root.resolve()),
        "root_aliases": [str(root.resolve())],
        "active_plan_id": None,
        "counts": {table: len(payload[table]) for table in TABLES},
        "state_hash": _state_hash(payload),
        "profile": profile,
        "contains_transcripts": contains_transcripts,
        "redacted": redacted,
        "processing": {"extract": {"pending": 0, "running": 0, "failed": 0, "completed": 0},
                       "embed": {"pending": 0, "running": 0, "failed": 0, "completed": 0},
                       "import": {"pending": 0, "running": 0, "failed": 0, "completed": 0}},
        "completeness": {"meaning": "committed_rows_only", "models_required": False,
                         "pending_analysis": 0, "failed_analysis": 0},
    }
    content_hash = _digest({
        **{k: v for k, v in captured.items() if k not in {"processing", "completeness"}},
        "objects": objects,
    })
    manifest = {
        "format": FORMAT, "format_version": VERSION, **captured, "objects": objects,
        "created_at": "2026-11-27T00:00:00+00:00",
        "parent_id": parent_id, "ancestors": ancestors or ([] if parent_id is None else [parent_id]),
        "content_hash": content_hash,
    }
    manifest["snapshot_id"] = _manifest_id(manifest)
    raw = _bytes(manifest)
    (out / "manifests" / f"{manifest['snapshot_id']}.json").write_bytes(raw)
    manifest_path = out / "manifest.json"
    manifest_path.write_bytes(raw)
    return manifest_path


# --- 1. Secrets scanner extensions, multi-line PEM, false positives, allowlist, redaction ---

def test_multiline_pem_and_false_positives_and_allowlist():
    pem = _sample_pem()
    assert [k for k, _ in scan_text(f"prefix\n{pem}\nsuffix", Rules())] == ["private_key"]

    # False-positive immunity: UUID, 40/64-char hex SHAs, uv.lock SHA-256, SRI digests
    safe_samples = [
        str(uuid4()),
        "a" * 40,
        "0123456789abcdef" * 4,
        "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=",
        "sha512-z4PhNX7vuL3xVChQ1m2AB9Yg5AULVxXcg/SpIdNs6c5H0NE8XYXysP+DGNKHfuwvY7kxvUdBeoGlODJ6+SfaPg==",
    ]
    for sample in safe_samples:
        assert scan_text(sample, Rules()) == [], sample
        assert not random_looking(sample), sample

    # [secrets] and [allowlist] in .cairnmind/publication.toml
    aws = _sample_aws()
    toml = (
        '[personal]\ndetect = ["email"]\n'
        '[secrets]\nentropy_bits = 4.2\ndetect_private_ip = true\ninternal_domains = [".corp.local"]\n'
        f'[allowlist]\nsha256 = ["{value_sha256(aws)}"]\npatterns = ["^10\\\\.0\\\\.0\\\\.1$"]\n'
    )
    rules = load_rules(toml, registered=["super-registered-secret-12345"])
    assert scan_text(f"Allowed doc key {aws} and allowlisted gateway 10.0.0.1", rules) == []
    found_kinds = [k for k, _ in scan_text("Host api.corp.local at 192.168.10.5 and admin@example.org", rules)]
    assert sorted(found_kinds) == ["email", "internal_domain", "private_ip"]
    # Registered secrets are NEVER allowlisted even if listed in [allowlist].values
    sneaky = load_rules('[allowlist]\nvalues = ["super-registered-secret-12345"]\n',
                        registered=["super-registered-secret-12345"])
    assert [k for k, _ in scan_text("leak: super-registered-secret-12345", sneaky)] == [REGISTERED]

    with pytest.raises(SecretRulesError):
        load_rules("[secrets]\nentropy_bits = 9.5\n")
    with pytest.raises(SecretRulesError):
        load_rules('[allowlist]\nsha256 = ["not-a-sha"]\n')


def test_redact_text_masks_with_typed_tags_and_is_idempotent():
    rules = load_rules('[personal]\ndetect = ["email"]\n[secrets]\ndetect_private_ip = true\n')
    text = f"Key {_sample_aws()}, token {_sample_jwt()}, mail dev@example.org, ip 10.20.30.40"
    redacted, counts = redact_text(text, rules)
    assert "[REDACTED:SECRET]" in redacted
    assert "[REDACTED:JWT]" in redacted
    assert "[REDACTED:EMAIL]" in redacted
    assert "[REDACTED:IP]" in redacted
    assert counts == {"[REDACTED:SECRET]": 1, "[REDACTED:JWT]": 1, "[REDACTED:EMAIL]": 1, "[REDACTED:IP]": 1}
    assert scan_text(redacted, rules) == []


# --- 2. .cairnmindignore, canonical git check-ignore, symlinks, binary files ---

def test_ignore_rules_negation_cairnmindignore_symlinks_and_binaries(tmp_path):
    root = _init_repo(tmp_path / "repo")
    (root / "secret.env").write_text("SECRET=1\n")
    (root / "important.env").write_text("PUBLIC=1\n")
    (root / "debug.log").write_text("log\n")
    (root / ".cairnmindignore").write_text("private/**\n*.internal.txt\n")
    (root / "private").mkdir()
    (root / "private" / "notes.md").write_text("do not index\n")
    (root / "keep.py").write_text("X = 1\n")
    (root / "secret.internal.txt").write_text("internal\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret\n")
    link = root / "escape.txt"
    has_symlink = True
    try:
        link.symlink_to(outside)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            has_symlink = False
        else:
            raise

    # Canonical git check-ignore respects negation !important.env
    git_ignored = git_check_ignore(root, ["secret.env", "important.env", "debug.log", "keep.py"])
    assert git_ignored is not None
    assert "secret.env" in git_ignored and "debug.log" in git_ignored
    assert "important.env" not in git_ignored and "keep.py" not in git_ignored

    matcher = IgnoreMatcher(root)
    assert matcher.is_ignored("private/notes.md")
    assert matcher.is_ignored("secret.internal.txt")
    assert matcher.is_ignored("secret.env")
    assert not matcher.is_ignored("important.env")
    assert not matcher.is_ignored("keep.py")

    # Symlink escaping repo detected and never walked by _files
    if has_symlink:
        assert escapes_root(link, root)
    assert not escapes_root(root / "keep.py", root)
    walked = {p.relative_to(root).as_posix() for p in _files(root)}
    assert "keep.py" in walked and "important.env" in walked
    assert "secret.env" not in walked and "debug.log" not in walked
    assert "private/notes.md" not in walked and "secret.internal.txt" not in walked
    if has_symlink:
        assert "escape.txt" not in walked

    # Binary detection (NUL byte)
    assert is_binary_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    assert not is_binary_bytes(b"def ok():\n    return 42\n")


# --- 3. Team vs full profiles, git_ref, working_tree_ref, fail-closed secrets & --redact ---

def test_team_profile_strips_transcripts_converts_git_ref_and_blocks_dirty_files(tmp_path):
    root = _init_repo(tmp_path / "repo")
    head_sha = _git(root, "rev-parse", "HEAD")
    pid = str(uuid4())

    committed_content = (root / "app.py").read_text(encoding="utf-8")
    code_src = _source_row(pid, str(root / "app.py"), committed_content, "code",
                           {"path": str(root / "app.py"), "relative_path": "app.py", "git_commit": head_sha})
    unref_transcript = _source_row(pid, "transcript://private/1", "Raw secret chat prompt", "transcript",
                                   {"normalized_text": "Raw secret chat prompt"})
    ref_transcript = _source_row(pid, "transcript://private/2", "Raw user prompt text", "transcript",
                                 {"normalized_text": "Raw user prompt text"})
    rec = _record_row(pid, "Use cookies for sessions.", [ref_transcript["id"]])
    payload = _make_payload(pid, [code_src, unref_transcript, ref_transcript], [rec])

    privacy = apply_snapshot_privacy(payload, root, profile="team")
    assert privacy["profile"] == "team"
    assert privacy["contains_transcripts"] is False
    assert privacy["git_refs"] == 1
    by_id = {s["id"]: s for s in payload["sources"]}
    # Unreferenced transcript completely dropped
    assert unref_transcript["id"] not in by_id
    # Referenced transcript has raw text and metadata stripped
    assert by_id[ref_transcript["id"]]["content"] == ""
    assert by_id[ref_transcript["id"]]["evidence_type"] == "transcript_ref_omitted"
    assert "normalized_text" not in by_id[ref_transcript["id"]]["metadata"]
    # Committed code converted to quote-by-reference (git_ref)
    gref = by_id[code_src["id"]]
    assert gref["evidence_type"] == "git_ref"
    assert gref["content"] == ""
    assert gref["file_path"] == "app.py"
    assert gref["commit_sha"] == head_sha
    assert gref["line_start"] == 1 and gref["line_end"] == 2
    assert gref["content_sha256"] == code_src["sha256"]

    # Dirty / uncommitted file blocks team export unless --skip-uncommitted
    dirty_src = _source_row(pid, str(root / "dirty.py"), "DIRTY = True\n", "code",
                            {"path": str(root / "dirty.py"), "relative_path": "dirty.py"})
    dirty_payload = _make_payload(pid, [dirty_src])
    with pytest.raises(SnapshotPrivacyError, match="skip-uncommitted"):
        apply_snapshot_privacy(dirty_payload, root, profile="team", skip_uncommitted=False)
    res = apply_snapshot_privacy(dirty_payload, root, profile="team", skip_uncommitted=True)
    assert res["skipped_uncommitted"] == ["dirty.py"]
    assert dirty_payload["sources"] == []

    # Full profile requires explicit include_transcripts=True
    with pytest.raises(ValueError, match="include-transcripts"):
        apply_snapshot_privacy(_make_payload(pid, [unref_transcript]), root, profile="full", include_transcripts=False)
    full_payload = _make_payload(pid, [unref_transcript])
    full_privacy = apply_snapshot_privacy(full_payload, root, profile="full", include_transcripts=True)
    assert full_privacy["contains_transcripts"] is True
    assert full_payload["sources"][0]["content"] == "Raw secret chat prompt"


def test_secrets_gate_blocks_fail_closed_and_redact_masks_snapshot_only(tmp_path):
    root = _init_repo(tmp_path / "repo")
    pid = str(uuid4())
    pem = _sample_pem()
    jwt = _sample_jwt()
    src = _source_row(pid, "note://leak", f"Deploy key:\n{pem}\nToken: {jwt}", "note")
    rec = _record_row(pid, f"Stored token {jwt}", [src["id"]])
    payload = _make_payload(pid, [src], [rec])

    # Fail-closed by default (never leaks secret text in error message)
    with pytest.raises(SnapshotPrivacyError) as exc_info:
        apply_snapshot_privacy(payload, root, profile="team", redact=False)
    msg = str(exc_info.value)
    assert "private key" in msg or "jwt" in msg
    assert "MIIEow" not in msg and "dozjgNry" not in msg

    # Opt-in --redact masks in place and produces an auditable package
    privacy = apply_snapshot_privacy(payload, root, profile="team", redact=True)
    assert privacy["redacted"] is True
    assert privacy["redaction_counts"]["[REDACTED:SECRET]"] >= 1
    assert privacy["redaction_counts"]["[REDACTED:JWT]"] >= 2
    manifest_path = _write_package(root, pid, payload, profile="team", redacted=True)
    assert inspect_snapshot(manifest_path)["status"] == "valid"
    report = audit_memory(root, check_history=False)
    assert report["status"] == "passed", report

    # ADR-08: memory publish NEVER accepts --redact (neither CLI nor HTTP schema)
    pub_cli = runner.invoke(cli.app, ["memory", "publish", "demo", "--redact"])
    assert pub_cli.exit_code == 2
    with pytest.raises(Exception):
        MemoryPublishBody(project="demo", redact=True)


# --- 4. Rebase / amend resilience -> stale_ref ---

def test_rebase_or_amend_marks_stale_ref_on_mismatch_and_resolves_when_head_matches(tmp_path):
    root = _init_repo(tmp_path / "repo")
    old_sha = _git(root, "rev-parse", "HEAD")
    orig_text = (root / "app.py").read_text(encoding="utf-8")
    orig_hash = hashlib.sha256(orig_text.encode()).hexdigest()
    pid = str(uuid4())

    def make_ref_payload() -> dict:
        src = _source_row(pid, str(root / "app.py"), orig_text, "code",
                          {"path": str(root / "app.py"), "relative_path": "app.py", "git_commit": old_sha})
        payload = _make_payload(pid, [src])
        apply_snapshot_privacy(payload, root, profile="team")
        return payload

    # Case A: history rewritten via orphan commit with SAME file content -> resolves from HEAD
    same_repo = tmp_path / "rewritten-same"
    same_repo.mkdir()
    _git(same_repo, "init", "-q", "-b", "main")
    _git(same_repo, "config", "user.name", "Rebase")
    _git(same_repo, "config", "user.email", "rebase@example.invalid")
    (same_repo / "app.py").write_text(orig_text, encoding="utf-8")
    _git(same_repo, "add", "app.py")
    _git(same_repo, "commit", "-qm", "rewritten same content")
    assert _git(same_repo, "rev-parse", "HEAD") != old_sha

    p_same = make_ref_payload()
    stale_same = resolve_snapshot_refs(p_same, same_repo)
    assert stale_same == []
    assert p_same["sources"][0]["content"] == orig_text
    assert p_same["sources"][0]["metadata"]["evidence_status"] == "head_matched"

    # Case B: history rewritten via orphan commit with DIFFERENT file content -> marked stale_ref
    diff_repo = tmp_path / "rewritten-diff"
    diff_repo.mkdir()
    _git(diff_repo, "init", "-q", "-b", "main")
    _git(diff_repo, "config", "user.name", "Rebase")
    _git(diff_repo, "config", "user.email", "rebase@example.invalid")
    (diff_repo / "app.py").write_text("VALUE = 999\n", encoding="utf-8")
    _git(diff_repo, "add", "app.py")
    _git(diff_repo, "commit", "-qm", "rewritten changed content")

    p_diff = make_ref_payload()
    stale_diff = resolve_snapshot_refs(p_diff, diff_repo)
    assert len(stale_diff) == 1
    assert stale_diff[0]["status"] == "stale_ref"
    assert stale_diff[0]["content_sha256"] == orig_hash
    assert p_diff["sources"][0]["content"] == ""
    assert p_diff["sources"][0]["metadata"]["evidence_status"] == "stale_ref"
    assert p_diff["sources"][0]["metadata"]["evidence_type"] == "stale_ref"


# --- 5. Deterministic snapshot merge ---

def test_snapshot_merge_combines_non_conflicting_branches_and_rejects_same_version_conflict(tmp_path):
    root = _init_repo(tmp_path / "repo")
    pid = str(uuid4())
    s_shared = _source_row(pid, "note://shared", "Shared constraint.", "note")
    r_shared_v1 = _record_row(pid, "Shared v1", [s_shared["id"]], key="shared", version=1)
    s_a = _source_row(pid, "note://branch-a", "Decision from branch A.", "note")
    r_a = _record_row(pid, "Use branch A.", [s_a["id"]], key="branch-a", version=1)
    s_b = _source_row(pid, "note://branch-b", "Decision from branch B.", "note")
    r_b = _record_row(pid, "Use branch B.", [s_b["id"]], key="branch-b", version=1)

    ours_p = _make_payload(pid, [s_shared, s_a], [r_shared_v1, r_a])
    theirs_p = _make_payload(pid, [s_shared, s_b], [r_shared_v1, r_b])
    ours_manifest = _write_package(root, pid, ours_p, subdir=tmp_path / "ours")
    theirs_manifest = _write_package(root, pid, theirs_p, subdir=tmp_path / "theirs")

    merged_dir = tmp_path / "merged"
    result = merge_snapshots(ours_manifest, theirs_manifest, merged_dir)
    assert result["status"] == "merged"
    assert result["counts"]["records"] == 3
    assert inspect_snapshot(merged_dir / "manifest.json")["status"] == "valid"

    # Conflicting edit to the same record ID at the same version raises MemoryConflict
    r_conflict = dict(r_shared_v1, content="Diverged v1 text")
    conflict_p = _make_payload(pid, [s_shared], [r_conflict])
    conflict_manifest = _write_package(root, pid, conflict_p, subdir=tmp_path / "conflict")
    with pytest.raises(MemoryConflict, match="Conflicting immutable record_revisions|Concurrent edit conflict"):
        merge_snapshots(ours_manifest, conflict_manifest, tmp_path / "out-conflict")


# --- 6. memory audit CLI (offline, transcripts, secrets, .cairnmindignore, Git history leak) ---

def test_memory_audit_cli_catches_transcripts_secrets_ignore_and_git_history_leak(tmp_path):
    root = _init_repo(tmp_path / "repo")
    pid = str(uuid4())
    unused = str(tmp_path / "no-data-dir")

    # 1. Commit a full snapshot with contains_transcripts=true into Git history
    t_src = _source_row(pid, "transcript://1", "Private conversation", "transcript")
    full_payload = _make_payload(pid, [t_src])
    _write_package(root, pid, full_payload, profile="full", contains_transcripts=True)
    _git(root, "add", ".memory/cairnmind")
    _git(root, "commit", "-qm", "accidental full snapshot")

    res_full = runner.invoke(cli.app, ["--data-dir", unused, "memory", "audit", str(root)])
    assert res_full.exit_code == 1
    assert "contains_transcripts" in res_full.output
    assert "does NOT remove transcripts from Git history" in res_full.output
    assert not (tmp_path / "no-data-dir").exists()

    # 2. Replace with a clean team snapshot in a newer commit -> audit passes (exit 0) BUT still warns about Git history!
    note_src = _source_row(pid, "note://clean", "Clean team note.", "note")
    rec = _record_row(pid, "Clean team decision.", [note_src["id"]])
    clean_payload = _make_payload(pid, [note_src], [rec])
    _write_package(root, pid, clean_payload, profile="team", contains_transcripts=False)
    _git(root, "add", ".memory/cairnmind")
    _git(root, "commit", "-qm", "replace with team snapshot")

    res_clean_json = runner.invoke(cli.app, ["--data-dir", unused, "memory", "audit", str(root), "--json"])
    assert res_clean_json.exit_code == 0, res_clean_json.output
    parsed = json.loads(res_clean_json.stdout)
    assert parsed["status"] == "passed"
    assert len(parsed["history_warnings"]) == 1
    assert parsed["history_warnings"][0]["code"] == "historical_transcript_leak"

    # 3. Secret inside snapshot -> exit 1, value never printed
    secret_src = _source_row(pid, "note://secret", f"Use key {_sample_aws()}", "note")
    _write_package(root, pid, _make_payload(pid, [secret_src]), profile="team")
    res_sec = runner.invoke(cli.app, ["--data-dir", unused, "memory", "audit", str(root), "--no-history"])
    assert res_sec.exit_code == 1
    assert "secret_detected" in res_sec.output
    assert _sample_aws() not in res_sec.output

    # 4. .cairnmindignore violation in snapshot -> exit 1
    (root / ".cairnmindignore").write_text("app.py\n", encoding="utf-8")
    head_sha = _git(root, "rev-parse", "HEAD")
    ignored_src = {
        **_source_row(pid, str(root / "app.py"), (root / "app.py").read_text(encoding="utf-8"), "code",
                      {"relative_path": "app.py"}),
        "content": "", "evidence_type": "git_ref", "file_path": "app.py",
        "commit_sha": head_sha, "line_start": 1, "line_end": 2,
        "content_sha256": hashlib.sha256((root / "app.py").read_text(encoding="utf-8").encode()).hexdigest(),
    }
    _write_package(root, pid, _make_payload(pid, [ignored_src]), profile="team")
    res_ign = runner.invoke(cli.app, ["--data-dir", unused, "memory", "audit", str(root), "--no-history"])
    assert res_ign.exit_code == 1
    assert "ignored_path" in res_ign.output


# --- 7. .pre-commit-hooks.yaml & install_pre_commit_hook ---

def test_pre_commit_hooks_yaml_and_idempotent_installer(tmp_path):
    hooks_yaml = ROOT / ".pre-commit-hooks.yaml"
    assert hooks_yaml.is_file()
    content = hooks_yaml.read_text(encoding="utf-8")
    assert PRE_COMMIT_HOOK_ID in content
    assert "cairnmind memory audit" in content

    repo = tmp_path / "proj"
    repo.mkdir()
    assert install_pre_commit_hook(repo) is True
    cfg = repo / ".pre-commit-config.yaml"
    assert PRE_COMMIT_HOOK_ID in cfg.read_text(encoding="utf-8")
    # Second call is a no-op
    assert install_pre_commit_hook(repo) is False

    # Existing config is preserved
    repo2 = tmp_path / "proj2"
    repo2.mkdir()
    existing = "repos:\n  - repo: https://github.com/pre-commit/pre-commit-hooks\n    rev: v4.6.0\n    hooks:\n      - id: trailing-whitespace\n"
    (repo2 / ".pre-commit-config.yaml").write_text(existing, encoding="utf-8")
    assert install_pre_commit_hook(repo2) is True
    merged = (repo2 / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "trailing-whitespace" in merged and PRE_COMMIT_HOOK_ID in merged
