"""Records worth re-checking after a code change. Read-only: no model, no service, no write.

``build_impact`` takes what changed in a worktree since a commit and follows two kinds of
stored links from the changed files and symbols to records:

- ``relates_to``: extraction linked the record to a file or to a symbol because the quoted
  evidence names it;
- ``supported_by``: the record cites a snapshot of the file as its evidence.

A link means "worth checking", not that the record is wrong. The report is derived each
time and stored nowhere, so repeating it cannot duplicate anything, and it never touches
a record, a task state or a check result.

What changed is taken from Git and from the files themselves, not from the index: tracked
files that differ from the commit, files Git does not track yet, and, inside a file, every
definition whose text (with its decorators) differs between the commit and the worktree.
Which record is linked to what is taken from the index, so it is as fresh as the last
indexing and the last extraction.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath

from cairnmind.brief import _git_out, _line, _scoped_records, _title
from cairnmind.evidence import _FILE_KINDS, _QUOTE, _metadata, evidence_states
from cairnmind.parsing import analyze_code

MAX_FILE_BYTES = 2 * 1024 * 1024
_STATUS = {"A": "added", "M": "modified", "D": "deleted", "T": "modified", "R": "renamed", "C": "added"}
_SYMBOL = {"modified": "its definition changed", "removed": "it is no longer defined there",
           "added": "it is newly defined there", "moved": "it is now defined in the renamed file",
           None: "its definition could not be compared"}


def _changes(root, commit):
    """``{path relative to root: {"change", ...}}``: tracked differences with renames detected, and untracked files."""
    raw = _git_out(root, "diff", "--name-status", "-M", "-z", "--relative", commit, "--")
    untracked = _git_out(root, "ls-files", "--others", "--exclude-standard", "-z", "--")
    if raw is None or untracked is None:
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
    for name in untracked.split("\0"):
        if name:
            changed.setdefault(name, {"change": "added", "untracked": True})
    return changed


def _definitions(name, text):
    """``{symbol name: sorted definition texts}``, or None when the file cannot be compared by symbol."""
    if text is None:
        return None
    analysis = analyze_code(Path(name), text)
    if analysis["language"] == "text" or any(str(error).startswith("Parser unavailable") for error in analysis["errors"]):
        return None
    # The parser counts lines by "\n" only; str.splitlines would also split at form feeds and the like.
    lines = text.split("\n")
    found = {}
    for symbol in analysis["symbols"]:
        start = symbol["line_start"] - 1
        # A decorator is part of what a definition means, but the parser's definition starts below it.
        top = scan = start
        while scan > 0 and lines[scan - 1].strip():
            scan -= 1
            if lines[scan].lstrip().startswith("@"):
                top = scan
        found.setdefault(symbol["name"], []).append("\n".join(lines[top:symbol["line_end"]]))
    return {key: sorted(value) for key, value in found.items()}


def _old_text(root, commit, name):
    spec = f"{commit}:./{name}"
    size = _git_out(root, "cat-file", "-s", spec)
    if size is None or not size.isdigit() or int(size) > MAX_FILE_BYTES:
        return None
    raw = _git_out(root, "show", spec)
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
    """Per changed path: ``{symbol name: how it changed}``, or None when symbols cannot be compared."""
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
        elif "renamed_to" in entry:
            # Nothing is defined at the earlier path any more, whether or not the text changed.
            result[name] = None if old is None else {
                key: "removed" if new is None or key not in new else "moved" if old[key] == new[key] else "modified"
                for key in old}
        elif old is None or new is None:
            result[name] = None
        else:
            result[name] = {key: "removed" if key not in new else "added" if key not in old else "modified"
                            for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    return result


def _relative(path, root, roots):
    """A stored absolute path as a path relative to ``root``, or None when it belongs to another registered root."""
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        return None
    candidate = Path(path)
    owners = [item for item in roots if candidate.is_relative_to(item)]
    # A file under a nested root or in a sibling worktree is that root's file, not this one's.
    if not owners or max(owners, key=lambda item: len(item.parts)) != root:
        return None
    return candidate.relative_to(root).as_posix()


def _batches(items, size=500):
    items = list(items)
    for offset in range(0, len(items), size):
        yield items[offset:offset + size]


def _change_text(entry):
    if "renamed_to" in entry:
        return f"renamed to {entry['renamed_to']}"
    if "renamed_from" in entry:
        return f"renamed from {entry['renamed_from']}"
    return "added (not tracked by Git yet)" if entry.get("untracked") else entry["change"]


def _count(number, noun):
    return f"{number} {noun}{'' if number == 1 else 's'}"


def build_impact(store, project_id, since, worktree=None):
    """The records linked to what changed in the worktree since commit ``since``, as rows and as text."""
    from cairnmind.memory_paths import bound_metadata, memory_bindings
    from cairnmind.verification import _root
    project = store.resolve_project(project_id)
    root = _root(store, project["id"], worktree)
    if not isinstance(since, str) or not since.strip() or since.startswith(("-", "^")):
        raise ValueError("--since needs a commit, a tag or a branch name")
    if _git_out(root, "rev-parse", "--is-inside-work-tree") != "true":
        raise ValueError(f"Git could not be read in {_line(root)}; impact needs a Git worktree")
    commit = _git_out(root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{since}^{{commit}}")
    # Anything but one object name (a negated revision, a range) would make the reads below mean something else.
    if not commit or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None:
        raise ValueError(f"Git does not know the commit {_line(since, 80)!r} in {_line(root)}")
    changed = _changes(root, commit)
    symbols = _symbol_changes(root, commit, changed)
    roots = {Path(row["path"]).resolve() for row in store.query(
        "SELECT path FROM project_roots WHERE project_id=:project", {"project": project["id"]})} | {root}
    bindings = memory_bindings(store, project["id"])

    def local_name(path):
        name = _relative(path, root, roots)
        if name is None and bindings and isinstance(path, str):
            # A snapshot imported from another machine keeps that machine's path; bind it to the local root.
            name = _relative(bound_metadata(store, project["id"], {"path": path}, bindings=bindings).get("path"),
                             root, roots)
        return name

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
        name = local_name(_metadata(node).get("path") or node.get("label"))
        if name not in changed:
            continue
        link = {"via": "relates_to", "path": name, "change": _change_text(changed[name]),
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
            "SELECT id,uri,kind,sha256,metadata,is_current,created_at FROM sources "
            "WHERE project_id=:project AND id IN :ids",
            {"project": project["id"], "ids": batch}) if row.get("kind") in _FILE_KINDS})
    digests = {}

    def worktree_digest(name):
        if name not in digests:
            text = _new_text(root, changed[name].get("renamed_to", name))
            digests[name] = None if text is None else hashlib.sha256(text.encode()).hexdigest()
        return digests[name]

    for record in records.values():
        for source_id in dict.fromkeys(record.get("source_ids") or []):
            source = sources.get(source_id) if isinstance(source_id, str) else None
            if source is None:
                continue
            name = local_name(_metadata(source).get("path") or source.get("uri"))
            if name not in changed:
                continue
            if "renamed_to" not in changed[name] and source.get("sha256") == worktree_digest(name):
                continue  # the record was written from the file as it is now: there is nothing newer to check
            add(record["id"], {"via": "supported_by", "path": name, "change": _change_text(changed[name]),
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
    # A rename is one changed file, known under two names.
    files = {name for name, entry in changed.items() if "renamed_to" not in entry}
    linked_files = {changed[link["path"]].get("renamed_to", link["path"]) for row in rows for link in row["links"]}
    lines = [f"Records worth re-checking in {_line(project['name'], 60)} ({_line(root)}) after the changes since "
             f"{_line(since, 60)} ({commit[:12]})",
             "A link means \"worth checking\", not that the record is wrong. Nothing was written. "
             "Links are as fresh as the last indexing."]
    if not changed:
        lines.append("Nothing in this worktree differs from that commit.")
    elif not rows:
        lines.append(f"No record is linked to the {_count(len(files), 'changed file')}.")
    for row in rows:
        state = f", {_line(row['task_state'], 30)}" if "task_state" in row else ""
        lines += ["", f"[{row['kind']}] {row['status']}{state}: {row['title']} ({_line(row['id'], 60)})"]
        for link in row["links"]:
            where = f"{_line(link['path'], 120)}, which was {_line(link['change'], 140)}"
            if link["via"] == "relates_to":
                proposed = "" if link["link_status"] == "observed" else f" (link {_line(link['link_status'], 20)})"
                if "symbol" in link:
                    lines.append(f"  - names {_line(link['symbol'], 80)} in {where}; "
                                 f"{_SYMBOL[link['symbol_change']]}{proposed}")
                else:
                    lines.append(f"  - names the file {where}{proposed}")
            else:
                evidence = ""
                if row.get("evidence_state") == "evidence_replaced":
                    evidence = f"; {_QUOTE[row['quote_survives']]}"
                elif row.get("evidence_state") == "orphaned":
                    evidence = "; no supporting source has a current version"
                snapshot = f" from {_line(link['snapshot'], 10)}" if link["snapshot"] else ""
                lines.append(f"  - rests on a snapshot{snapshot} of {where}{evidence}")
    if changed and rows:
        rest = len(files) - len(linked_files)
        lines += ["", f"{_count(len(rows), 'record')} linked to {len(linked_files)} of "
                      f"{_count(len(files), 'changed file')}"
                      + (f"; {_count(rest, 'changed file')} with no linked record" if rest else "")]
    return {"project": {"id": project["id"], "name": project["name"]}, "worktree": str(root), "since": since,
            "commit": commit,
            "changed_files": [{"path": name, **entry, "symbols": symbols.get(name)}
                              for name, entry in sorted(changed.items(), key=lambda item: PurePosixPath(item[0]).parts)],
            "records": rows, "text": "\n".join(lines)}
