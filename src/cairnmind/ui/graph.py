"""Safe repository file-context reader, quote locator, and 4-column provenance graph builder."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any

from fastapi import HTTPException

MAX_CONTEXT_FILE_BYTES = 1_048_576
MAX_CONTEXT_LINES = 400


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_git_segment(parts: tuple[str, ...]) -> bool:
    return any(part.casefold() == ".git" for part in parts)


def safe_read_file_context(
    repo_root: str | Path,
    relative_path: str,
    line_start: int = 1,
    line_end: int = 60,
) -> dict[str, Any]:
    """Read a bounded, path-sanitized line window inside `repo_root` with live disk SHA-256."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise HTTPException(400, "path is required")
    if "\0" in relative_path:
        raise HTTPException(400, "Invalid path")
    normalized = relative_path.replace("\\", "/").strip()
    candidate_rel = Path(normalized)
    if candidate_rel.is_absolute() or ".." in candidate_rel.parts or _is_git_segment(candidate_rel.parts):
        raise HTTPException(403, "Path must stay inside repository root")
    root = Path(repo_root).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(404, "Repository root does not exist")
    target = (root / candidate_rel).resolve()
    try:
        inside = target.is_relative_to(root)
    except ValueError:
        inside = False
    if not inside or _is_git_segment(target.relative_to(root).parts):
        raise HTTPException(403, "Path escapes repository root")
    if not target.is_file():
        raise HTTPException(404, f"File not found: {normalized}")
    try:
        size = target.stat().st_size
    except OSError:
        raise HTTPException(404, "Cannot inspect file") from None
    if size > MAX_CONTEXT_FILE_BYTES:
        raise HTTPException(413, "File exceeds 1 MiB inspection limit")
    try:
        raw = target.read_bytes()
    except OSError:
        raise HTTPException(500, "Cannot read file") from None
    if b"\x00" in raw[:8192]:
        raise HTTPException(415, "Binary files cannot be displayed as quote context")
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    all_lines = text.split("\n")
    total_lines = len(all_lines)
    start = max(1, min(int(line_start), total_lines))
    end = max(start, min(int(line_end), total_lines, start + MAX_CONTEXT_LINES - 1))
    lines = [{"number": idx, "text": all_lines[idx - 1]} for idx in range(start, end + 1)]
    return {
        "path": target.relative_to(root).as_posix(),
        "line_start": start,
        "line_end": end,
        "total_lines": total_lines,
        "current_disk_sha256": _sha256_bytes(raw),
        "lines": lines,
    }


def _classify_reason_code(message: str) -> str:
    lower = message.lower()
    if any(tok in lower for tok in ("resume_required", "ack", "packet", "conflict", "stale")):
        return "OCC_STALE_RESUME_PACKET"
    if "quote" in lower or "evidence" in lower:
        return "VERBATIM_QUOTE_NOT_FOUND"
    if "fingerprint" in lower or "hash" in lower:
        return "FILE_FINGERPRINT_MISMATCH"
    if "test" in lower or "check" in lower or "verification" in lower:
        return "TEST_GATE_UNVERIFIED"
    return "OCC_STALE_RESUME_PACKET"


def _find_quote_lines(file_path: Path, quote: str) -> tuple[int, int]:
    if not quote or not file_path.is_file():
        return 1, 25
    try:
        if file_path.stat().st_size > MAX_CONTEXT_FILE_BYTES:
            return 1, 25
        text = file_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    except OSError:
        return 1, 25
    idx = text.find(quote.replace("\r\n", "\n"))
    if idx < 0:
        return 1, min(30, max(1, text.count("\n") + 1))
    start = text[:idx].count("\n") + 1
    end = start + quote.replace("\r\n", "\n").count("\n")
    return start, max(start, end)


def _build_graph_from_entities(claims: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    seen_edges: set[str] = set()

    def add_node(node: dict[str, Any]) -> None:
        if node["id"] not in seen_nodes:
            seen_nodes.add(node["id"])
            nodes.append(node)

    def add_edge(source: str, target: str, kind: str, status: str) -> None:
        eid = f"{source}->{target}"
        if eid not in seen_edges:
            seen_edges.add(eid)
            edges.append({"id": eid, "source": source, "target": target, "kind": kind, "status": status})

    for claim in claims:
        path = claim.get("source_path")
        ev_status = "REJECTED" if claim.get("drift_kind") == "QUOTE_MUTATED" else claim.get("provenance_status", "VERIFIED")
        if path:
            add_node({
                "id": f"file:{path}",
                "column": 0,
                "category": "source_file",
                "label": Path(path).name,
                "subtitle": path,
                "status": ev_status,
                "ref_id": path,
                "meta": {"sha256_short": (claim.get("current_disk_sha256") or "")[:7]},
            })
        ev_id = f"ev:quote:{claim['id']}"
        add_node({
            "id": ev_id,
            "column": 1,
            "category": "evidence",
            "label": f"Quote L{claim['line_start']}-{claim['line_end']}",
            "subtitle": claim.get("drift_kind", "MATCH"),
            "status": ev_status,
            "ref_id": claim["id"],
            "meta": {"evidence_type": "quote", "source_path": path or ""},
        })
        if path:
            add_edge(f"file:{path}", ev_id, "anchors_quote", ev_status)
        claim_nid = f"claim:{claim['id']}"
        claim_origin = claim.get("origin") or {}
        add_node({
            "id": claim_nid,
            "column": 2,
            "category": "claim",
            "label": claim["key"],
            "subtitle": claim["title"],
            "status": claim.get("provenance_status", "VERIFIED"),
            "ref_id": claim["id"],
            "meta": {
                "kind": claim["kind"],
                "version": claim["version"],
                "origin_category": claim_origin.get("category", "ENGINE_VERIFIED"),
                "origin_evidence": claim_origin.get("origin_evidence", "verified"),
                "origin_client": claim_origin.get("origin_client"),
            },
        })
        add_edge(ev_id, claim_nid, "supports_claim", ev_status)

    for task in tasks:
        gate = task["test_gate"]
        task_status = (
            "REJECTED" if gate["status"] == "FAIL"
            else "STALE" if task["evidence_status"] == "STALE" or gate["status"] in {"STALE_CODE", "MISSING"}
            else "VERIFIED"
        )
        tnid = f"task:{task['id']}"
        task_origin = task.get("origin") or {}
        add_node({
            "id": tnid,
            "column": 3,
            "category": "task",
            "label": f"{task['id']} · {task['state']}",
            "subtitle": task["title"],
            "status": task_status,
            "ref_id": task["id"],
            "meta": {
                "profile": gate["profile_name"],
                "gate_status": gate["status"],
                "origin_category": task_origin.get("category", "ENGINE_VERIFIED"),
                "origin_evidence": task_origin.get("origin_evidence", "verified"),
                "origin_client": task_origin.get("origin_client"),
            },
        })
        for cid in task.get("claim_ids") or []:
            if f"claim:{cid}" in seen_nodes:
                add_edge(f"claim:{cid}", tnid, "informs_task", task_status)

    return {"nodes": nodes, "edges": edges}
