"""Transcript target capture, generation matching, tail completion, recovery, and archive proofs."""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cairnmind.memory.sessions_packets import AGENT_CLIENTS, _hash, _scope_contains
from cairnmind.storage.domain_types import SessionRecord
from cairnmind.storage.sql import utc_now
from cairnmind.storage.storage_query import asc, db_find, db_find_one, db_update_where, desc, eq, is_in, lt


class _SessionArchiveMixin:
    """Capture-horizon discovery, generation merging, tail finishing, recovery, and archive proofs."""

    if TYPE_CHECKING:
        store: Any
        settings: Any

        def _session(self, session_id: str) -> dict[str, Any]: ...
        def _require_scope(self, tx: Any, project_id: str, root: str) -> None: ...
        def _own_continuity(self, session: SessionRecord | Mapping[str, Any]) -> list[str]: ...

    @staticmethod
    def _encode_watermark(watermark: Any) -> Any:
        """Encode 64-bit unsigned device/inode values for ArcadeDB JSON storage (#441)."""
        if not isinstance(watermark, dict):
            return watermark
        from cairnmind.pal.private_files import decode_file_id, encode_file_id
        encoded = dict(watermark)
        for key in ("device", "inode"):
            if key in encoded and decode_file_id(encoded[key]) is not None:
                encoded[key] = encode_file_id(encoded[key])
        return encoded

    @staticmethod
    def _target(path: str, client: str, watermark: dict | None = None) -> dict:
        from cairnmind.pal.private_files import decode_file_id, encode_file_id
        result: dict[str, Any] = {"path": str(Path(path).expanduser().resolve()), "client": client}
        if isinstance(watermark, dict):
            for key in ("size", "device", "inode", "mtime_ns", "head_sha256", "head_bytes",
                        "observed_size", "pending_tail_bytes", "pending_tail_sha256", "stable"):
                if key in watermark:
                    val = watermark[key]
                    if key in ("device", "inode") and decode_file_id(val) is not None:
                        val = encode_file_id(val)
                    result[key] = val
        if "size" not in result:
            try:
                from cairnmind.memory.integrations import _transcript_watermark
                result.update(_transcript_watermark(Path(result["path"])))
            except (OSError, ValueError) as exc:
                result["error"] = f"Transcript cannot be captured: {type(exc).__name__}"
        size = result.get("size")
        if type(size) is not int or size < 0:
            result.setdefault("error", "Transcript EOF is unknown")
        return result

    @staticmethod
    def _same_generation(left: dict, right: dict) -> bool:
        """Short captured prefixes may grow on append without changing generation."""
        from cairnmind.pal.private_files import decode_file_id
        left_dev, left_ino = decode_file_id(left.get("device")), decode_file_id(left.get("inode"))
        right_dev, right_ino = decode_file_id(right.get("device")), decode_file_id(right.get("inode"))
        if (left.get("path"), left_dev, left_ino) != (right.get("path"), right_dev, right_ino):
            return False
        left_size = max(int(left.get("size", -1)), int(left.get("observed_size", -1)))
        right_size = max(int(right.get("size", -1)), int(right.get("observed_size", -1)))
        if left_size != right_size:
            try:
                actual = Path(left["path"]).stat()
                if actual.st_size < max(left_size, right_size) or (actual.st_dev, actual.st_ino) != (left_dev, left_ino):
                    return False
            except OSError:
                return False
        # A complete-prefix hash excludes a captured unfinished record. Never
        # merge away that separately bound requirement after its bytes change.
        tails = [row for row in (left, right) if row.get("pending_tail_sha256")]
        if tails:
            try:
                with Path(left["path"]).open("rb") as handle:
                    actual = os.fstat(handle.fileno())
                    if (actual.st_dev, actual.st_ino) != (left_dev, left_ino):
                        return False
                    for row in tails:
                        start = row.get("captured_tail_start", row.get("size"))
                        end = row.get("observed_size")
                        if (type(start) is not int or type(end) is not int
                                or not 0 < end - start <= 2 * 1024 * 1024):
                            return False
                        handle.seek(start)
                        raw = handle.read(end - start)
                        if (len(raw) != end - start
                                or hashlib.sha256(raw).hexdigest() != row["pending_tail_sha256"]):
                            return False
            except OSError:
                return False
        if (left.get("head_bytes"), left.get("head_sha256")) == (
                right.get("head_bytes"), right.get("head_sha256")):
            return True
        if left_dev is None or left_ino is None:
            return False
        try:
            with Path(left["path"]).open("rb") as handle:
                actual = os.fstat(handle.fileno())
                if (actual.st_dev, actual.st_ino) != (left_dev, left_ino):
                    return False
                head = handle.read(4096)
            return all(type(row.get("head_bytes")) is int and 0 <= row["head_bytes"] <= len(head)
                       and hashlib.sha256(head[:row["head_bytes"]]).hexdigest() == row.get("head_sha256")
                       for row in (left, right))
        except OSError:
            return False

    @classmethod
    def _merge_targets(cls, result: list[dict]) -> list[dict]:
        targets: list[dict] = []
        for target in result:
            for index, previous in enumerate(targets):
                if target.get("kind") == "agent_journal" or previous.get("kind") == "agent_journal":
                    if (target.get("kind") != previous.get("kind")
                            or target.get("agent_session_id") != previous.get("agent_session_id")
                            or target.get("path") != previous.get("path")
                            or target.get("error") or previous.get("error")):
                        continue
                    # A validated append extends one local journal generation.
                    # Equal-length conflicting digests must stay separately visible.
                    if target.get("sequence") == previous.get("sequence") and target.get("sha256") != previous.get("sha256"):
                        continue
                    if target.get("sequence") != previous.get("sequence"):
                        smaller = min((target, previous), key=lambda row: row.get("sequence", -1))
                        try:
                            amount = smaller.get("size")
                            if type(amount) is not int or not 0 <= amount <= 64 * 1024 * 1024:
                                continue
                            from cairnmind.pal.private_files import open_no_follow
                            fd = open_no_follow(smaller["path"], os.O_RDONLY, private_on_create=False)
                            with os.fdopen(fd, "rb") as handle:
                                prefix = handle.read(amount)
                            if len(prefix) != amount or hashlib.sha256(prefix).hexdigest() != smaller.get("sha256"):
                                continue
                        except OSError:
                            continue
                    chosen = dict(max((target, previous), key=lambda row: row.get("sequence", -1)))
                    known = [row for row in (target, previous)
                             if row.get("proof", {}).get("status") == "complete"]
                    known.extend(row["known_prefix"] for row in (target, previous)
                                 if row.get("known_prefix", {}).get("proof", {}).get("status") == "complete")
                    full = next((row for row in known if row.get("sequence") == chosen.get("sequence")
                                 and row.get("sha256") == chosen.get("sha256")), None)
                    if full:
                        chosen["proof"] = full["proof"]
                        chosen.pop("known_prefix", None)
                    elif known:
                        prefix = max(known, key=lambda row: row.get("sequence", -1))
                        chosen["known_prefix"] = {key: prefix[key] for key in
                            ("kind", "path", "client", "agent_session_id", "sequence", "size", "sha256", "chain_hash", "proof")
                            if key in prefix}
                    targets[index] = chosen
                    break
                if cls._same_generation(previous, target):
                    required = max(int(target.get("size", -1)), int(target.get("observed_size", -1)))
                    previous_required = max(int(previous.get("size", -1)), int(previous.get("observed_size", -1)))
                    if required > previous_required or (required == previous_required
                                                        and target.get("proof") and not previous.get("proof")):
                        targets[index] = target
                    break
            else:
                targets.append(target)
        return sorted(targets, key=lambda row: (row["path"], str(row.get("device")),
                                                str(row.get("inode")), str(row.get("head_sha256"))))

    def _targets(self, session: dict, deadline: float) -> list[dict]:
        project, root = session["project_id"], session["worktree"]
        result = []
        rows = db_find(self.store, "sessions", {"project_id": project, "worktree": root})
        for row in rows:
            result.extend(row.get("transcript_targets", []))
            if row.get("client") in AGENT_CLIENTS:
                from cairnmind.agents.agent_memory import capture_agent_target
                captured = capture_agent_target(self.settings, row)
                if captured is not None:
                    result.append(captured)
        cursors = db_find(self.store, "import_cursors", {"project_id": project})
        for cursor in cursors:
            if time.monotonic() > deadline:
                raise TimeoutError("Resume preparation deadline exceeded")
            metadata_rows = db_find(
                self.store,
                "sources",
                [eq("project_id", project), eq("metadata.import_path", cursor["path"])],
                columns=("metadata",),
                limit=1,
            )
            cwd = (metadata_rows[0].get("metadata") or {}).get("cwd") if metadata_rows else None
            if cwd and not _scope_contains(root, cwd):
                continue
            captured = self._target(cursor["path"], cursor.get("client", "auto"))
            if captured.get("error") and any(
                    row.get("path") == cursor["path"] and row.get("proof", {}).get("status") == "complete"
                    and row.get("size") == int(cursor.get("offset", 0)) for row in result):
                continue
            result.append(captured)
        # A durable hook must count even before the independent scanner drains it.
        spool = self.settings.data_dir / "hook-events"
        for path in sorted(spool.glob("*.json")):
            if time.monotonic() > deadline:
                raise TimeoutError("Resume preparation deadline exceeded")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not _scope_contains(root, payload.get("cwd")):
                    continue
                if payload.get("transcript_path"):
                    result.append(self._target(payload["transcript_path"], payload.get("client", "auto"),
                                               payload.get("transcript_watermark")))
            except (OSError, ValueError, TypeError):
                # Unreadable spool metadata cannot prove that its horizon was archived.
                result.append({"path": str(path), "client": "unknown",
                               "error": "Unreadable pending hook event; archive horizon is unknown"})
        # Preserve different generations and the largest captured horizon of each.
        return self._merge_targets(result)

    @staticmethod
    def _finish_captured_tail(target: dict) -> None:
        """Wait only for the record containing the original finite observed tail."""
        if not target.get("pending_tail_bytes"):
            return
        size, observed = target["size"], target.get("observed_size")
        if (type(observed) is not int or observed <= size
                or observed - size != target["pending_tail_bytes"]
                or observed - size > 2 * 1024 * 1024):
            raise ValueError("Captured unfinished JSONL tail has invalid bounds")
        with Path(target["path"]).open("rb") as handle:
            from cairnmind.pal.private_files import decode_file_id
            actual = os.fstat(handle.fileno())
            if ((target.get("device") is not None and decode_file_id(target["device"]) != actual.st_dev)
                    or (target.get("inode") is not None and decode_file_id(target["inode"]) != actual.st_ino)):
                raise ValueError("Captured transcript generation was rotated or truncated")
            head = handle.read(int(target.get("head_bytes", min(4096, size))))
            if target.get("head_sha256") and hashlib.sha256(head).hexdigest() != target["head_sha256"]:
                raise ValueError("Captured transcript prefix changed")
            handle.seek(size)
            original = handle.read(observed - size)
            if (len(original) != observed - size
                    or hashlib.sha256(original).hexdigest() != target.get("pending_tail_sha256")):
                raise ValueError("Captured unfinished JSONL tail changed")
            completion = handle.readline(2 * 1024 * 1024 + 1)
            if not completion.endswith(b"\n") or len(completion) > 2 * 1024 * 1024:
                raise ValueError("Captured EOF contains an incomplete JSONL range")
            completed = observed + len(completion)
            handle.seek(0)
            head = handle.read(min(4096, completed))
        target.update(size=completed, captured_tail_start=size, pending_tail_bytes=0, head_bytes=len(head),
                      head_sha256=hashlib.sha256(head).hexdigest(), tail_completed_at=completed)

    @staticmethod
    def _packet_targets(packet: dict) -> list[dict]:
        """Recover a pre-upgrade action's original horizons, including empty scope."""
        targets = []
        for archive in packet.get("archive_watermarks", []):
            target = dict(archive)
            if archive.get("status") == "complete":
                target["proof"] = {key: archive.get(key) for key in
                                   ("size", "status", "sha256", "archived_offset", "source_count", "source_ids_hash")}
                if archive.get("kind") == "agent_journal":
                    target["proof"].update({key: archive.get(key) for key in
                                           ("public_sources_hash", "sequence", "chain_hash")})
            targets.append(target)
        return targets

    def _legacy_action_targets(self, session: dict, packet: dict) -> list[dict] | None:
        """Recover an old launch boundary only when its native receipt proves it.

        Releases before fixed horizons could replace a ready launch packet with
        a partial refresh that chased its own newly created rollout. The first
        durable SessionStart identifies the already prepared action. A later
        lifecycle boundary prevents this inference; its requirements are kept.
        """
        events = db_find(
            self.store,
            "session_events",
            [eq("session_id", session["id"]), is_in("hook_event_name", ("SessionStart", "PreCompact"))],
            order_by=(asc("created_at"),),
        )
        if len(events) == 1:
            event = events[0]
            if (event.get("hook_event_name") == "SessionStart"
                    and event.get("resume_reason") in {"startup", "resume"}
                    and event.get("native_session_id")
                    and event.get("native_session_id") == session.get("native_session_id")):
                candidates = db_find(
                    self.store,
                    "resume_packets",
                    {"session_id": session["id"], "epoch": event.get("applied_epoch")},
                    order_by=(desc("created_at"),),
                )
                for row in candidates:
                    prepared = row.get("payload") or {}
                    if (row.get("created_at", "") <= event.get("created_at", "")
                            and prepared.get("status") == "ready"
                            and not prepared.get("omitted_archive_watermarks")):
                        return self._packet_targets(prepared)
        if (not packet.get("omitted_archive_watermarks")
                and (packet.get("status") == "ready" or packet.get("archive_watermarks"))):
            return self._packet_targets(packet)
        return None

    def _recover_capture_target(self, session: dict, target: dict, deadline: float) -> dict | None:
        """Resolve a never-captured startup request from a later durable receipt.

        No physical EOF is recaptured here. A bounded target, partial tail or
        generation conflict is never replaced by this compatibility repair.
        The caller still has to prove every byte of the fixed later watermark.
        """
        allowed = {"path", "client", "error", "status", "archived_offset", "unknowns"}
        if (set(target) - allowed or target.get("error") not in {
                "Transcript EOF is unknown", "Transcript cannot be captured: FileNotFoundError"}
                or target.get("client") not in {"codex", "claude"}
                or not isinstance(target.get("path"), str)
                or type(target.get("archived_offset", 0)) is not int or target.get("archived_offset", 0) != 0
                or ("status" in target and target["status"] != "partial")
                or ("unknowns" in target and target["unknowns"] != [target["error"]])):
            return None
        path = str(Path(target["path"]).expanduser().resolve())
        if path != target["path"]:
            return None
        events = db_find(
            self.store,
            "session_events",
            {"project_id": session["project_id"], "client": target["client"], "transcript_path": path},
            order_by=(asc("created_at"), asc("id")),
        )
        bound = []
        for event in events:
            if time.monotonic() >= deadline:
                raise TimeoutError("Resume preparation deadline exceeded")
            native = event.get("native_session_id")
            owner = db_find_one(self.store, "sessions", {"id": event.get("session_id")})
            if (not isinstance(native, str) or not native or not owner
                    or (owner.get("project_id"), owner.get("client"), owner.get("worktree"),
                        owner.get("native_session_id")) !=
                    (session["project_id"], target["client"], session["worktree"], native)):
                continue
            bound.append(event)
        # Without an event ID in the legacy marker, multiple failed captures
        # cannot safely be attributed to one finite later receipt.
        missing = [event for event in bound if event.get("transcript_watermark") is None]
        if len(missing) != 1 or missing[0].get("hook_event_name") != "SessionStart":
            return None
        origin = missing[0]
        captured = next((event for event in bound
                         if event.get("session_id") == origin["session_id"]
                         and event.get("native_session_id") == origin["native_session_id"]
                         and isinstance(event.get("created_at"), str)
                         and isinstance(origin.get("created_at"), str)
                         and event["created_at"] > origin["created_at"]
                         and event.get("transcript_watermark") is not None), None)
        if captured is None:
            return None
        watermark = captured["transcript_watermark"]
        if not isinstance(watermark, dict):
            return None
        from cairnmind.pal.private_files import decode_file_id
        size, device, inode = (watermark.get("size"), watermark.get("device"), watermark.get("inode"))
        head_bytes, head_hash = watermark.get("head_bytes"), watermark.get("head_sha256")
        observed, pending = watermark.get("observed_size", size), watermark.get("pending_tail_bytes", 0)
        if (type(size) is not int or size <= 0
                or decode_file_id(device) is None or decode_file_id(inode) is None
                or type(head_bytes) is not int or head_bytes != min(4096, size)
                or not isinstance(head_hash, str) or len(head_hash) != 64
                or any(char not in "0123456789abcdef" for char in head_hash)
                or type(observed) is not int or observed < size
                or type(pending) is not int or pending < 0 or observed - size != pending):
            return None
        candidate = self._target(path, target["client"], watermark)
        if candidate.get("error"):
            return None
        event_fields = ("id", "session_id", "native_session_id", "hook_event_name", "created_at")
        candidate["capture_recovery"] = {
            "original_target": dict(target), "project_id": session["project_id"], "worktree": session["worktree"],
            "native_session_id": origin["native_session_id"],
            "origin_event": {key: origin.get(key) for key in event_fields},
            "resolution_event": {key: captured.get(key) for key in event_fields},
        }
        return candidate

    def _archive(self, session: dict, target: dict, deadline: float) -> dict:
        """Prove every captured line against durable source hashes, not cursor alone."""
        if target.get("kind") == "agent_journal":
            from cairnmind.agents.agent_memory import archive_agent_target
            return archive_agent_target(self.store, self.settings, session, target, deadline)
        result = {**target, "status": "partial", "archived_offset": 0, "unknowns": []}
        if target.get("error"):
            candidate = self._recover_capture_target(session, target, deadline)
            if candidate is not None:
                checked = self._archive(session, candidate, deadline)
                if checked["status"] == "complete":
                    target.clear()
                    target.update(candidate)
                    return checked
                result["unknowns"].extend(checked.get("unknowns", []))
            result["unknowns"].append(target["error"])
            return result
        try:
            self._finish_captured_tail(target)
            result.update(target)
        except (OSError, ValueError) as exc:
            result["unknowns"].append(str(exc))
            return result
        path, size = Path(target["path"]), target["size"]
        try:
            from cairnmind.pal.private_files import decode_file_id
            stat = path.stat()
            if ((target.get("device") is not None and decode_file_id(target["device"]) != stat.st_dev)
                    or (target.get("inode") is not None and decode_file_id(target["inode"]) != stat.st_ino)
                    or stat.st_size < size):
                raise ValueError("Captured transcript generation was rotated or truncated")
            if target.get("head_sha256"):
                with path.open("rb") as handle:
                    head = handle.read(int(target.get("head_bytes", min(4096, size))))
                if hashlib.sha256(head).hexdigest() != target["head_sha256"]:
                    raise ValueError("Captured transcript prefix changed")
        except (OSError, ValueError) as exc:
            if not target.get("proof"):
                result["unknowns"].append(str(exc))
                return result
        cursor = db_find_one(self.store, "import_cursors", {"path": str(path), "project_id": session["project_id"]})
        if not cursor or int(cursor.get("offset", 0)) < size:
            result["archived_offset"] = int(cursor.get("offset", 0)) if cursor else 0
            result["unknowns"].append("Import cursor has not archived the captured EOF")
            return result
        # Compatibility repairs only reinterpret already archived, hash-checked
        # technical records. Run before proof so the first launch after an
        # upgrade does not race the independent scanner's next import pass.
        from cairnmind.scip.ingest import _identifier, reparse_history_sources
        try:
            reparsed = reparse_history_sources(self.store, path, session["project_id"],
                                               target.get("client", "auto"), deadline=deadline)
        except (OSError, ValueError) as exc:
            result["unknowns"].append(f"Transcript compatibility repair failed: {exc}")
            return result
        sources = db_find(
            self.store,
            "sources",
            [
                eq("project_id", session["project_id"]),
                eq("metadata.import_path", str(path)),
                lt("metadata.byte_offset", size),
            ],
            columns=("id", "uri", "sha256", "kind", "metadata"),
        )
        by_offset = {}
        for row in sources:
            metadata = row.get("metadata") or {}
            by_offset.setdefault(metadata.get("byte_offset"), []).append(row)
        source_ids, unknowns, digest, offset = [], list(reparsed.get("errors", [])), hashlib.sha256(), 0
        # A kind a person accepted as technical for this project does not block (#210); nothing else does.
        from cairnmind.memory.record_kinds import accepted, blocking_message, unknown_kind
        standing, named, first = accepted(self.store, session["project_id"]), set(), None
        try:
            from cairnmind.pal.private_files import decode_file_id
            stat = path.stat()
            if ((target.get("device") is not None and decode_file_id(target["device"]) != stat.st_dev)
                    or (target.get("inode") is not None and decode_file_id(target["inode"]) != stat.st_ino)
                    or stat.st_size < size):
                raise ValueError("Captured transcript generation was rotated or truncated")
            with path.open("rb") as handle:
                head_bytes = int(target.get("head_bytes", min(4096, size)))
                head = handle.read(head_bytes)
                if target.get("head_sha256") and hashlib.sha256(head).hexdigest() != target["head_sha256"]:
                    raise ValueError("Captured transcript prefix changed")
                handle.seek(0)
                while offset < size:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Resume preparation deadline exceeded")
                    raw = handle.readline(size - offset)
                    if not raw or not raw.endswith(b"\n"):
                        raise ValueError("Captured EOF contains an incomplete JSONL range")
                    sha = hashlib.sha256(raw.decode("utf-8", "replace").encode("utf-8")).hexdigest()
                    candidates = [row for row in by_offset.get(offset, []) if row.get("sha256") == sha]
                    if not candidates:
                        raise ValueError(f"Missing durable source at transcript byte {offset}")
                    source = sorted(candidates, key=lambda row: row["id"])[0]
                    first = raw.decode("utf-8", "replace") if offset == 0 else first
                    if source["kind"] != "transcript" or (source.get("metadata") or {}).get("schema_warning"):
                        found = unknown_kind(source, raw.decode("utf-8", "replace"), first)
                        if found is None:
                            unknowns.append(f"Unparsed or conflicting transcript record at byte {offset}")
                        elif (found["client"], found["kind"]) not in standing | named:
                            named.add((found["client"], found["kind"]))
                            unknowns.append(blocking_message(found, offset, session["project_id"]))
                    recovery = target.get("capture_recovery")
                    if recovery:
                        metadata = source.get("metadata") or {}
                        native = recovery["native_session_id"]
                        client = metadata.get("client")
                        cwd = metadata.get("cwd")
                        if (metadata.get("session_id") != native or client not in {"auto", target["client"]}
                                or source.get("uri") != f"transcript://{client}/{_identifier(path, native)}/{offset}/{sha}"
                                or (cwd is not None and not _scope_contains(session["worktree"], cwd))):
                            unknowns.append(f"Capture recovery source provenance conflicts at byte {offset}")
                    source_ids.append(source["id"])
                    digest.update(raw)
                    offset += len(raw)
            result.update(archived_offset=offset, sha256=digest.hexdigest(), source_count=len(source_ids),
                          source_ids_hash=_hash(sorted(source_ids)), unknowns=unknowns,
                          status="partial" if unknowns else "complete")
        except (OSError, ValueError) as exc:
            # An already-proved immutable horizon survives deletion of the original
            # transcript, provided the complete durable source set still exists.
            proof = target.get("proof")
            if proof and proof.get("size") == size and proof.get("status") == "complete":
                ids = sorted(row["id"] for row in sources)
                if len(ids) == proof.get("source_count") and _hash(ids) == proof.get("source_ids_hash"):
                    return {**result, **proof, "path": str(path), "original_unavailable": True}
            result["unknowns"].append(str(exc))
        return result

    def archive_agent_journal(self, session_id: str) -> dict:
        """Archive a finite own-runtime journal without granting model authority."""
        from cairnmind.agents.agent_memory import capture_agent_target
        session = self._session(session_id)
        if session.get("client") not in AGENT_CLIENTS:
            raise ValueError("Only an API or Copilot session owns an agent journal")
        continuity = self._own_continuity(session)
        if continuity:
            return {"session_id": session_id, "status": "partial", "source_ids": [], "unknowns": continuity}
        target = capture_agent_target(self.settings, session)
        if target is None:
            return {"session_id": session_id, "status": "complete", "sequence": 0,
                    "source_ids": [], "unknowns": []}
        rows = db_find(
            self.store, "sessions", {"project_id": session["project_id"], "worktree": session["worktree"]},
            columns=("transcript_targets",),
        )
        known = [value for row in rows for value in row.get("transcript_targets", [])
                 if value.get("kind") == "agent_journal" and value.get("agent_session_id") == session_id]
        merged = self._merge_targets([*known, target])
        if len(merged) != 1:
            return {"session_id": session_id, "status": "partial", "source_ids": [],
                    "unknowns": ["Conflicting own-agent captured journal horizons; start a new agent session."]}
        target = merged[0]
        archive = self._archive(session, target, time.monotonic() + 30)
        if archive.get("status") == "complete":
            proved = {**target, "proof": {key: archive.get(key) for key in
                       ("size", "status", "sha256", "archived_offset", "source_count", "source_ids_hash",
                        "public_sources_hash", "sequence", "chain_hash")}}
            with self.store.transaction() as tx:
                self._require_scope(tx, session["project_id"], session["worktree"])
                current = db_find_one(tx, "sessions", {"id": session_id}, required=True)
                if current is None:
                    raise LookupError(f"Unknown session: {session_id}")
                targets = self._merge_targets([*current.get("transcript_targets", []), proved])
                db_update_where(tx, "sessions", set_fields={"transcript_targets": targets, "updated_at": utc_now()},
                                where={"id": session_id})
        return {"session_id": session_id, **archive}
