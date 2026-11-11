"""Records worth re-checking after a code change. Read-only: no model, no service, no write.

``build_impact`` takes what changed in a worktree since a commit and follows two kinds of
stored links from the changed files and symbols to records:

- ``relates_to``: extraction linked the record to a file or to a symbol because the quoted
  evidence names it;
- ``supported_by``: the record cites a snapshot of the file as its evidence.

A link means "worth checking", not that the record is wrong. The report is derived each
time and stored nowhere, so repeating it cannot duplicate anything, and it never touches
a record, a task state or a check result.

What changed is taken from Git and from the files themselves, not from the index: a symbol
counts as changed when the text of its definition differs between the commit and the
worktree. Which record is linked to what is taken from the index, so it is as fresh as the
last indexing and the last extraction.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from cairnmind.brief import _git_out, _line, _scoped_records, _title
from cairnmind.evidence import _FILE_KINDS, _QUOTE, _metadata, evidence_states
from cairnmind.parsing import analyze_code

MAX_FILE_BYTES = 2 * 1024 * 1024
_STATUS = {"A": "added", "M": "modified", "D": "deleted", "T": "modified", "R": "renamed", "C": "added"}


def _changes(root, commit):
    """``{path relative to root: {"change", "renamed_to" or "renamed_from"}}`` from Git, with renames detected."""
    raw = _git_out(root, "diff", "--name-status", "-M", "-z", "--relative", commit, "--")
    if raw is None:
        raise ValueError("Cannot compare the worktree with that commit")
    parts = [part for part in raw.split("\0") if part]
    changed, index = {}, 0
    while index < len(parts):
        code = parts[index][:1]
        if code in "RC" and index + 2 < len(parts):
            old, new = parts[index + 1], parts[index + 2]
            if code == "R":
                changed[old] = {"change": "renamed", "renamed_to": new}
                changed[new] = {"change": "renamed", "renamed_from": old}
            else:
                changed[new] = {"change": "added"}
            index += 3
        elif index + 1 < len(parts):
            changed[parts[index + 1]] = {"change": _STATUS.get(code, "modified")}
            index += 2
        else:
            break
    return changed


def _definitions(name, text):
    """``{symbol name: sorted definition texts}``, or None when the file cannot be compared by symbol."""
    if text is None:
        return None
    analysis = analyze_code(Path(name), text)
    if analysis["language"] == "text" or any(str(error).startswith("Parser unavailable") for error in analysis["errors"]):
        return None
    lines = text.splitlines()
    found = {}
    for symbol in analysis["symbols"]:
        found.setdefault(symbol["name"], []).append("\n".join(lines[symbol["line_start"] - 1:symbol["line_end"]]))
    return {key: sorted(value) for key, value in found.items()}


def _old_text(root, commit, name):
    raw = _git_out(root, "show", f"{commit}:./{name}")
    return raw if raw is not None and "\0" not in raw else None


def _new_text(root, name):
    path = root / name
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
        return None if b"\0" in raw else raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _symbol_changes(root, commit, changed):
    """Per changed path: ``{symbol name: "modified" | "removed" | "added"}``, or None when symbols cannot be compared."""
    result = {}
    for name, entry in changed.items():
        if "renamed_from" in entry:
            old_name, new_name = entry["renamed_from"], name
        elif "renamed_to" in entry:
            old_name, new_name = name, entry["renamed_to"]
        else:
            old_name = new_name = name
        old = _definitions(old_name, None if entry["change"] == "added" else _old_text(root, commit, old_name))
        new = _definitions(new_name, None if entry["change"] == "deleted" else _new_text(root, new_name))
        if entry["change"] == "deleted":
            result[name] = None if old is None else {key: "removed" for key in old}
        elif entry["change"] == "added":
            result[name] = None if new is None else {key: "added" for key in new}
        elif old is None or new is None:
            result[name] = None
        else:
            result[name] = {key: "removed" if key not in new else "added" if key not in old else "modified"
                            for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    return result


def _relative(path, roots):
    """A stored absolute path as a path relative to the registered root that contains it."""
    if not isinstance(path, str) or not path:
        return None
    candidate = Path(path)
    for root in roots:
        try:
            return candidate.relative_to(root).as_posix()
        except ValueError:
            continue
    return None


def _batches(items, size=500):
    items = list(items)
    for offset in range(0, len(items), size):
        yield items[offset:offset + size]


def _change_text(name, entry):
    if "renamed_to" in entry:
        return f"renamed to {entry['renamed_to']}"
    if "renamed_from" in entry:
        return f"renamed from {entry['renamed_from']}"
    return entry["change"]


def build_impact(store, project_id, since, worktree=None):
    """The records linked to what changed in the worktree since commit ``since``, as rows and as text."""
    from cairnmind.verification import _root
    project = store.resolve_project(project_id)
    root = _root(store, project["id"], worktree)
    if not isinstance(since, str) or not since.strip() or since.startswith("-"):
        raise ValueError("--since needs a commit, a tag or a branch name")
    commit = _git_out(root, "rev-parse", "--verify", "--quiet", f"{since}^{{commit}}")
    if not commit:
        raise ValueError(f"Git does not know the commit {since!r} in {root}")
    changed = _changes(root, commit)
    symbols = _symbol_changes(root, commit, changed)
    roots = sorted({Path(row["path"]).resolve() for row in store.query(
        "SELECT path FROM project_roots WHERE project_id=:project", {"project": project["id"]})} | {root},
        key=lambda item: len(item.parts), reverse=True)
    records = {record["id"]: record for record in _scoped_records(store, project["id"], root)
               if record.get("status") != "superseded"}
    links: dict[str, list[dict]] = {}

    def add(record_id, link):
        if link not in links.setdefault(record_id, []):
            links[record_id].append(link)

    # Links that extraction drew from a record to a file or a symbol named in its evidence.
    edges = [edge for edge in store.query(
        "SELECT from_id,to_id,status FROM edges WHERE project_id=:project AND kind='relates_to'",
        {"project": project["id"]}) if str(edge.get("from_id", "")).startswith("record:")
        and edge["from_id"][7:] in records]
    nodes = {}
    for batch in _batches(sorted({edge["to_id"] for edge in edges})):
        nodes.update({row["id"]: row for row in store.query(
            "SELECT id,kind,label,metadata FROM nodes WHERE project_id=:project AND id IN :ids",
            {"project": project["id"], "ids": batch})})
    for edge in edges:
        node = nodes.get(edge["to_id"])
        if not node or node.get("kind") not in ("file", "symbol"):
            continue
        name = _relative(_metadata(node).get("path") or node.get("label"), roots)
        if name not in changed:
            continue
        link = {"via": "relates_to", "path": name, "change": _change_text(name, changed[name]),
                "link_status": edge.get("status") or "observed"}
        if node["kind"] == "symbol":
            known = symbols.get(name)
            if known is not None and node["label"] not in known:
                continue  # the file changed, but the definition this record names did not
            link.update(symbol=node["label"], symbol_change=None if known is None else known[node["label"]])
        add(edge["from_id"][7:], link)

    # Records that cite a snapshot of a changed file as their evidence.
    cited = sorted({source_id for record in records.values() for source_id in record.get("source_ids") or []
                    if isinstance(source_id, str)})
    sources = {}
    for batch in _batches(cited):
        sources.update({row["id"]: row for row in store.query(
            "SELECT id,uri,kind,metadata,is_current,created_at FROM sources WHERE project_id=:project AND id IN :ids",
            {"project": project["id"], "ids": batch}) if row.get("kind") in _FILE_KINDS})
    for record in records.values():
        for source_id in dict.fromkeys(record.get("source_ids") or []):
            source = sources.get(source_id)
            if source is None:
                continue
            name = _relative(_metadata(source).get("path") or source.get("uri"), roots)
            if name in changed:
                add(record["id"], {"via": "supported_by", "path": name, "change": _change_text(name, changed[name]),
                                   "snapshot": str(source.get("created_at") or "")[:10],
                                   "snapshot_is_current": bool(source.get("is_current"))})
    states = evidence_states(store, project["id"], [records[record_id] for record_id in links
                                                    if any(link["via"] == "supported_by" for link in links[record_id])])
    rows = []
    for record_id in sorted(links, key=lambda item: (records[item]["kind"], item)):
        record = records[record_id]
        row = {"id": record_id, "kind": record["kind"], "status": record["status"], "title": _title(record),
               "links": sorted(links[record_id], key=lambda link: (link["via"], link["path"], link.get("symbol") or ""))}
        if record["kind"] == "task":
            row["task_state"] = _metadata(record).get("state", "todo")
        if record_id in states:
            row.update(evidence_state=states[record_id]["evidence_state"],
                       quote_survives=states[record_id]["quote_survives"])
        rows.append(row)
    linked_paths = {link["path"] for row in rows for link in row["links"]}
    short = commit[:12]
    lines = [f"Records worth re-checking in {_line(project['name'], 60)} ({_line(root)}) after the changes since "
             f"{_line(since, 60)} ({short})",
             "A link means \"worth checking\", not that the record is wrong. Nothing was written. "
             "Links are as fresh as the last indexing."]
    if not changed:
        lines.append("Nothing differs from that commit in the tracked files of this worktree.")
    elif not rows:
        lines.append(f"No record is linked to the {len(changed)} changed file{'' if len(changed) == 1 else 's'}.")
    for row in rows:
        state = f", {row['task_state']}" if "task_state" in row else ""
        lines += ["", f"[{row['kind']}] {row['status']}{state}: {row['title']} ({_line(row['id'], 60)})"]
        for link in row["links"]:
            where = f"{_line(link['path'], 120)}, which was {link['change']}"
            if link["via"] == "relates_to":
                proposed = "" if link["link_status"] == "observed" else f" (link {_line(link['link_status'], 20)})"
                if "symbol" in link:
                    what = {"modified": "its definition changed", "removed": "it is no longer defined there",
                            "added": "it is newly defined there",
                            None: "its definition could not be compared"}[link["symbol_change"]]
                    lines.append(f"  - names {_line(link['symbol'], 80)} in {where}; {what}{proposed}")
                else:
                    lines.append(f"  - names the file {where}{proposed}")
            else:
                evidence = ""
                if row.get("evidence_state") == "evidence_replaced":
                    evidence = f"; {_QUOTE[row['quote_survives']]}"
                elif row.get("evidence_state") == "orphaned":
                    evidence = "; no supporting source has a current version"
                snapshot = f" from {link['snapshot']}" if link["snapshot"] else ""
                lines.append(f"  - rests on a snapshot{snapshot} of {where}{evidence}")
    if changed and rows:
        rest = len(changed) - len(linked_paths)
        lines += ["", f"{len(rows)} record{'' if len(rows) == 1 else 's'} linked to {len(linked_paths)} of "
                      f"{len(changed)} changed file{'' if len(changed) == 1 else 's'}"
                      + (f"; {rest} changed file{'' if rest == 1 else 's'} with no linked record" if rest else "")]
    return {"project": {"id": project["id"], "name": project["name"]}, "worktree": str(root), "since": since,
            "commit": commit,
            "changed_files": [{"path": name, **entry, "symbols": symbols.get(name)}
                              for name, entry in sorted(changed.items(), key=lambda item: PurePosixPath(item[0]).parts)],
            "records": rows, "text": "\n".join(lines)}
