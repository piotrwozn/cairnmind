"""Publishing a project's confirmed knowledge as one file per record or plan (#170).

Only states a person's act covers travel: a review audit whose hash matches the version (a review, a
conflict decision or confirm_plan), a workspace-form finding saved as confirmed, and mechanical progress
or supersession after one. Proposed wordings, transcripts, file contents, check runs and every
verification field stay on this machine. Rendering is deterministic, and a file is written only when
its bytes change, so publishing twice with nothing new touches no file. Cairnmind never stages, commits
or pushes; the files are committed by a person with their branch.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

from .memory_paths import PUBLISHED_RELATIVE
from .memory_published_check import STATIC_FILES, check_files, format_bytes, read_tree, scan
from .memory_published_release import SecretsRefused, publication_rules, release_sources
from .memory_published_secrets import described
from .memory_published_format import (
    GITATTRIBUTES, GITIGNORE, MAX_NOTE_CHARS, canonical_uuid, MAX_QUOTE_CHARS, PUBLISHED_KINDS, J, _audit, _when, clean_text,
    clean_value, derive_covered, derive_plan_covered, path_of, plan_hash, projection, ref_class, render, same,
    sha256_text, task_fields, MAX_FILE_BYTES,
)
from .sql import clean_row, one_row, update_document
from .store import degraded_origin

# Writing stayed behind this switch until the secrets gate of #171 refused registered secrets and private
# keys. It is no longer read; the name is kept for scripts and tests that still set it.
PUBLICATION_SWITCH = "CAIRNMIND_ENABLE_PUBLICATION"
_BATCH = 500


class Held(Exception):
    """A record or plan that is not published, with the reason `memory publish` prints."""


def _rows(reader, sql, ids, params=None):
    ids, rows = sorted(set(ids)), []
    for start in range(0, len(ids), _BATCH):
        rows += [clean_row(row) for row in reader.query(sql, {**(params or {}), "ids": ids[start:start + _BATCH]})]
    return rows


def source_ref(source, root):
    """(reference, passage) of a source as it travels: never an absolute path, a passage only for notes and reviews."""
    metadata = source.get("metadata") or {}
    if source["kind"] == "published":
        ref = metadata.get("published")
        return ref, (source["content"] if ref_class(ref) in ("note", "review", "release") else None)
    at, uri = source["created_at"], str(source.get("uri", ""))
    if source["kind"] in ("code", "document", "file"):
        path = metadata.get("relative_path")
        if not isinstance(path, str):
            try:
                path = Path(os.path.normpath(str(metadata.get("path") or uri))).relative_to(root).as_posix()
            except ValueError:
                raise Held(f"source {source['id']} is a file outside the project root") from None
        ref, passage = {"at": at, "commit": metadata.get("git_commit"), "kind": source["kind"], "path": path,
                        "sha256": source["sha256"], "snapshot": metadata.get("snapshot")}, None
    elif source["kind"] == "manual" and metadata.get("origin") == "workspace_form":
        if len(source["content"]) > MAX_NOTE_CHARS:
            raise Held(f"note {source['id']} is longer than {MAX_NOTE_CHARS} characters")
        ref, passage = {"actor_name": metadata.get("actor_name"), "at": at, "branch": metadata.get("branch"),
                        "commit": metadata.get("commit"), "kind": "note", "origin": "workspace_form"}, source["content"]
    elif uri.startswith("review://") and metadata.get("origin") == "local_user_review":
        ref, passage = {"at": at, "kind": "review"}, source["content"]
    elif uri.startswith("release://") and metadata.get("origin") == "local_user_release":
        ref, passage = {"at": at, "kind": "release"}, source["content"]  # A person's release (#171).
    elif source["kind"] == "plan":
        ref, passage = {"at": at, "kind": "plan"}, None
    else:
        ref, passage = {"at": at, "kind": source["kind"], "origin": metadata.get("origin")}, None
    if not clean_value(ref) or (passage is not None and not clean_text(passage)):
        raise Held(f"source {source['id']} holds control or bidi characters; fix it before publishing")
    return ref, passage


def quote_of(evidence, cited, sources):
    """The stored quote, with its lines, when it occurs in a cited file or note; otherwise None (withheld)."""
    if not isinstance(evidence, str) or not evidence.strip():
        return None
    quote = evidence.replace("\r\n", "\n")
    if len(quote) > MAX_QUOTE_CHARS or not clean_text(quote):
        return None
    for source_id in cited:
        source = sources.get(source_id)
        if source is None:
            continue
        metadata = source.get("metadata") or {}
        if source["kind"] in ("code", "document", "file") or (
                source["kind"] == "manual" and metadata.get("origin") == "workspace_form"):
            content = (source.get("content") or "").replace("\r\n", "\n")
            if quote in content:
                first = content.split(quote, 1)[0].count("\n") + 1
                return {"source": source_id, "lines": [first, first + quote.count("\n")], "text": quote}
        elif source["kind"] == "published":
            for known in metadata.get("published_quotes") or []:
                if isinstance(known, dict) and known.get("sha256") == sha256_text(quote):
                    return {"source": source_id, "lines": list(known["lines"]), "text": quote}
    return None


def _is_plan_source(source):
    return source["kind"] == "plan" or (source["kind"] == "published"
                                        and ref_class((source.get("metadata") or {}).get("published")) == "plan")


def version_projection(row, sources):
    """P of one stored record version: what a published revision says."""
    cited = [s for s in dict.fromkeys(row.get("source_ids") or []) if s in sources]
    plan_sources = sorted((s for s in cited if _is_plan_source(sources[s])),
                          key=lambda s: (sources[s]["created_at"], s))
    cited = sorted({s for s in cited if not _is_plan_source(sources[s])} | set(plan_sources[:1]))
    metadata = row.get("metadata") or {}
    value = {"status": row["status"], "key": row["key"], "supersedes": row.get("supersedes"),
             "sources": cited, "quote": quote_of(metadata.get("evidence"), cited, sources), "text": row["content"]}
    if row["kind"] == "task":
        value.update(task_fields(metadata))
    return value


def select_history(kind, record_id, versions, sources, refs):
    """The record's published revisions, from its stored versions in order."""
    published = []
    for version in versions:
        mark = (version.get("metadata") or {}).get("publication")
        if (isinstance(mark, dict) and mark.get("version") == version["version"]
                and type(mark.get("revision")) is int and 1 <= mark["revision"] <= len(published) + 1):
            # A version the importer wrote as published revision k (a fast-forward, or --take-published after a
            # conflict): the history follows the published one from here, and local revisions it replaced stay local.
            del published[mark["revision"] - 1:]
        if version["status"] == "proposed" or set(version.get("source_ids") or []) - set(sources):
            continue
        value = version_projection(version, sources)
        previous = published[-1] if published else None
        if previous is not None and same(projection(previous), value):
            continue
        cited = {source_id: refs(source_id) for source_id in value["sources"]}
        covered = derive_covered(kind, record_id, previous, value, cited, len(published) + 1)
        if covered is None:
            continue
        if not clean_value(value):
            raise Held("its text holds control or bidi characters; fix it before publishing")
        published.append({"at": version["updated_at"], "covered": covered, **value})
    return published


def plan_definitions(plan, revisions, sources, refs, audit_ids):
    """The plan's published definitions: each distinct definition a confirm_plan act covers, in order."""
    cited = {s for row in revisions for s in row["snapshot"]["plan"].get("source_ids", [])} | set(audit_ids)
    plan_sources = sorted((s for s in cited if s in sources and _is_plan_source(sources[s])),
                          key=lambda s: (sources[s]["created_at"], s))
    if not plan_sources:
        return []
    audits = {}
    for source_id in cited:
        if source_id in sources:
            audit = _audit({"ref": refs(source_id)[0], "passage": refs(source_id)[1]}) \
                if ref_class(refs(source_id)[0]) == "review" else None
            if audit and audit.get("action") == "confirm_plan" and audit.get("plan_id") == plan["id"]:
                audits[source_id] = audit
    definitions = []
    for row in sorted(revisions, key=lambda r: r["version"]):
        snapshot = row["snapshot"]
        core = {"title": snapshot["plan"]["title"], "goal": snapshot["plan"]["goal"],
                "constraints": snapshot["plan"]["constraints"],
                "steps": [[task["id"], task["metadata"].get("step_key")] for task in snapshot["tasks"]]}
        previous = definitions[-1] if definitions else None
        if previous is not None and same(core, {k: previous[k] for k in core}):
            continue
        after = _when(audits[previous["covered"]["source"]], previous["covered"]["source"]) if previous else None
        matches = [_when(audit, s) for s, audit in audits.items()
                   if audit.get("plan_sha256") == plan_hash(core) and (after is None or _when(audit, s) > after)]
        if not matches:
            continue
        chosen = min(matches)[1]
        definition = {"at": audits[chosen].get("at"), "covered": None, **core,
                      "sources": sorted(set((previous["sources"] if previous else []) + [plan_sources[0], chosen]))}
        covered = derive_plan_covered(plan["id"], previous, definition,
                                      {s: dict(zip(("ref", "passage"), refs(s), strict=True)) for s in definition["sources"]})
        if covered is None:
            continue
        if not clean_value(core) or not isinstance(definition["at"], str):
            raise Held("its definition holds control or bidi characters")
        definitions.append({**definition, "covered": covered})
    return definitions


class Publisher:
    def __init__(self, store):
        self.store = store

    def destination(self, project_id, worktree=None):
        from .memory import MemoryManager
        project, root = MemoryManager(self.store)._scope(project_id, worktree)
        return project, root, root / PUBLISHED_RELATIVE

    @contextmanager
    def _lock(self, project_id):
        folder = self.store.data_dir / "runtime"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(folder / f"publish-{project_id}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _collect(self, tx, project_id):
        records = [clean_row(r) for r in tx.query("SELECT FROM records WHERE project_id=:project AND kind IN :kinds",
                                                  {"project": project_id, "kinds": list(PUBLISHED_KINDS)})]
        versions = {}
        for row in _rows(tx, "SELECT FROM record_revisions WHERE project_id=:project AND record_id IN :ids",
                         [r["id"] for r in records], {"project": project_id}):
            versions.setdefault(row["record_id"], []).append(row["snapshot"])
        plans = [clean_row(r) for r in tx.query("SELECT FROM plans WHERE project_id=:project", {"project": project_id})]
        plan_revisions = {}
        for row in [clean_row(r) for r in tx.query("SELECT FROM plan_revisions WHERE project_id=:project",
                                                   {"project": project_id})]:
            plan_revisions.setdefault(row["plan_id"], []).append(row)
        audits = {}
        for row in tx.query("SELECT id,metadata.plan_id AS plan FROM sources WHERE project_id=:project "
                            "AND metadata.origin='local_user_review' AND metadata.plan_id IS NOT NULL",
                            {"project": project_id}):
            audits.setdefault(row["plan"], []).append(row["id"])
        wanted = {s for rows in versions.values() for row in rows for s in row.get("source_ids") or []}
        wanted |= {s for rows in plan_revisions.values() for row in rows
                   for s in row["snapshot"]["plan"].get("source_ids", [])}
        wanted |= {s for ids in audits.values() for s in ids}
        sources = {row["id"]: row for row in _rows(
            tx, "SELECT id,project_id,uri,kind,sha256,metadata,created_at FROM sources WHERE id IN :ids", wanted)
            if row.get("project_id") == project_id}
        bodies = [s for s, row in sources.items() if row["kind"] in ("code", "document", "file", "manual", "published")
                  or str(row.get("uri", "")).startswith("review://")]
        for row in _rows(tx, "SELECT id,content FROM sources WHERE id IN :ids", bodies):
            sources[row["id"]]["content"] = row["content"]
        for row in versions.values():
            row.sort(key=lambda r: r["version"])
        return records, versions, plans, plan_revisions, audits, sources

    def build(self, project_id, worktree=None, authorize=None):
        """Render the package from one consistent read: {path: bytes}, the models, and what is held."""
        project, root, folder = self.destination(project_id, worktree)
        with self.store.transaction() as tx:
            if authorize is not None:
                authorize()
            records, versions, plans, plan_revisions, audits, sources = self._collect(tx, project_id)
            releases = release_sources(tx, project_id)
        sources.update({s: row for rows in releases.values() for s, row in rows.items()})
        cache = {}

        def refs(source_id):
            if source_id not in cache:
                cache[source_id] = source_ref(sources[source_id], root)
            return cache[source_id]

        def as_source(source_id):
            ref, passage = refs(source_id)
            return {"ref": ref, "passage": passage}

        histories, held, heads = {}, {}, {r["id"]: r for r in records}

        def model_of(kind, identifier, created_at, key, entries):
            cited = {s for entry in entries for s in entry["sources"]}
            if kind != "plan":
                cited |= set(releases.get(identifier, ()))  # Releases travel beside the sources, cited by no revision.
            return {"kind": kind, "id": identifier, "created_at": created_at,
                    "sources": {s: as_source(s) for s in sorted(cited)}, key: entries}

        for record in records:
            try:
                if not canonical_uuid(record["id"]):
                    raise Held("its ID is not a canonical UUID")
                if degraded_origin(record.get("metadata")):
                    raise Held("an external or third-party session wrote it (degraded origin)")
                history = select_history(record["kind"], record["id"], versions.get(record["id"], []), sources,
                                         as_source)
                if not history:
                    raise Held("no person's act covers it; review it in the inspector")
                if any(degraded_origin(sources[s].get("metadata")) for r in history for s in r["sources"]):
                    raise Held("it cites a source of degraded origin")
                _writable(model_of(record["kind"], record["id"], record["created_at"], "revisions", history))
                histories[record["id"]] = history
            except Held as exc:
                held[record["id"]] = (record["kind"], str(exc))
        definitions, held_plans = {}, {}
        for plan in plans:
            try:
                found = plan_definitions(plan, plan_revisions.get(plan["id"], []), sources, refs, audits.get(plan["id"], []))
                if not found:
                    raise Held("no confirm_plan act covers it")
                if not canonical_uuid(plan["id"]):
                    raise Held("its ID is not a canonical UUID")
                _writable(model_of("plan", plan["id"], plan["created_at"], "definitions", found))
                definitions[plan["id"]] = found
            except Held as exc:
                held_plans[plan["id"]] = str(exc)
        self._close(histories, held, definitions, held_plans, heads)
        models = [model_of(heads[i]["kind"], i, heads[i]["created_at"], "revisions", h) for i, h in sorted(histories.items())]
        models += [model_of("plan", plan["id"], plan["created_at"], "definitions", definitions[plan["id"]])
                   for plan in plans if plan["id"] in definitions]
        files = {path_of(model): render(model) for model in models}
        held.update({plan_id: ("plan", reason) for plan_id, reason in held_plans.items()})
        return {"project": project, "root": root, "folder": folder, "files": files, "models": models, "held": held}

    @staticmethod
    def _close(histories, held, definitions, held_plans, heads):
        """Hold, to a fixpoint, whatever names a record or plan that is not published itself."""
        changed = True
        while changed:
            changed = False
            for record_id, history in list(histories.items()):
                reason = None
                for revision in history:
                    names = [revision["supersedes"], *revision.get("depends_on", [])]
                    missing = next((n for n in names if n and n not in histories), None)
                    if missing:
                        reason = f"it names record {missing}, which is not published"
                    elif revision.get("plan") and revision["plan"] not in definitions:
                        reason = f"its plan {revision['plan']} is not published"
                if reason is None and history[-1]["covered"]["by"] == "superseded" \
                        and not Publisher._replaced(record_id, history, histories, definitions, heads):
                    history.pop()
                    reason = None if history else "it was superseded by a record that is not published"
                    changed = True
                if reason:
                    held[record_id] = (heads[record_id]["kind"], reason)
                    del histories[record_id]
                    changed = True
            for plan_id, found in list(definitions.items()):
                missing = next((s[0] for d in found for s in d["steps"] if s[0] not in histories), None)
                if missing:
                    held_plans[plan_id] = f"its step {missing} is not published"
                    del definitions[plan_id]
                    changed = True

    @staticmethod
    def _replaced(record_id, history, histories, definitions, heads):
        """A mechanical supersession travels only with what replaced it, as a reader sees it in the tree
        (memory_published_check.replaced_in_tree): a published record that names it, or a published plan.
        A conflict decision recorded on both records instead is a review, which covers the loser itself."""
        metadata = heads[record_id].get("metadata") or {}
        if any(r["supersedes"] == record_id for other in histories.values() for r in other):
            return True
        plan = history[-1].get("plan")
        return bool(metadata.get("plan_archived") and plan in definitions
                    and record_id not in [s[0] for s in definitions[plan][-1]["steps"]])

    def publish(self, project_id, worktree=None, *, preview=False, authorize=None):
        from .memory import MemoryConflict, _sync_directory, _write_whole
        with self._lock(project_id):
            built = self.build(project_id, worktree, authorize)
            folder, files, project = built["folder"], built["files"], built["project"]
            # The secrets gate (#171), before any file is written: refuse, never mask, and print no value.
            rules = publication_rules(built["root"], self.store.settings)
            hits = scan({"records": {m["id"]: m for m in built["models"] if m["kind"] != "plan"},
                         "plans": {m["id"]: m for m in built["models"] if m["kind"] == "plan"},
                         "paths": {m["id"]: path_of(m) for m in built["models"]}}, rules)
            if hits:
                raise SecretsRefused("Publishing refused, nothing was written: " + "; ".join(
                    described(hit) for hit in hits[:50]) + (f"; and {len(hits) - 50} more" if len(hits) > 50 else "")
                    + ". Remove the value, or read the record and release it with `cairnmind memory release`.")
            if any(p.is_symlink() for p in (built["root"] / ".memory", folder)):
                raise ValueError("Memory publication needs a real project directory")
            existing, errors = read_tree(folder) if folder.exists() else ({}, [])
            if errors:
                raise MemoryConflict("The published memory cannot be read: " + _listing(errors))
            identity = project.get("publication_id")
            if "FORMAT.json" in existing:
                tree, errors = check_files(existing, rules)
                if errors:
                    raise MemoryConflict("The published memory is invalid; fix or import it first: " + _listing(errors))
                if tree["project_id"] != identity:
                    raise MemoryConflict(f"The published memory here belongs to {tree['project_id']}; import it first")
                self._compare(tree, built)
            elif len(existing) > sum(name in existing for name in STATIC_FILES):
                raise MemoryConflict("The published memory has files but no FORMAT.json; inspect it first")
            identity = identity or project_id
            merged = {**existing, **files, "FORMAT.json": existing.get("FORMAT.json", format_bytes(identity)),
                      ".gitattributes": existing.get(".gitattributes", GITATTRIBUTES),
                      ".gitignore": existing.get(".gitignore", GITIGNORE)}
            _, errors = check_files(merged, rules)
            if errors:
                raise MemoryConflict("Publishing would leave an invalid tree: " + _listing(errors))
            changed = sorted(path for path, data in merged.items() if existing.get(path) != data)
            result = {"status": "preview" if preview else "written" if changed else "unchanged",
                      "destination": str(folder), "project_id": identity, "written": changed if not preview else [],
                      "would_write": changed if preview else [], "unchanged": len(merged) - len(changed),
                      "files": len(merged), "bytes": sum(len(data) for data in merged.values()),
                      "records": sum(m["kind"] != "plan" for m in built["models"]),
                      "plans": sum(m["kind"] == "plan" for m in built["models"]),
                      "held": _held(built["held"]), "git": "nothing was staged, committed or pushed"}
            if preview or not changed:
                return result
            for path in changed:
                _write_whole(folder / path, merged[path])
            for directory in sorted({(folder / path).parent for path in changed}):
                _sync_directory(directory)
            if not project.get("publication_id"):
                with self.store.transaction() as tx:
                    update_document(tx, "projects", {"publication_id": identity}, "id=:id", {"id": project_id})
            return result

    @staticmethod
    def _compare(tree, built):
        """Existing revision blocks are never rewritten: each file there must be a prefix of what this store publishes."""
        from .memory import MemoryConflict
        ours = {model["id"]: model for model in built["models"]}
        behind = []
        for identifier, path in sorted(tree["paths"].items()):
            theirs, mine = tree["records"].get(identifier) or tree["plans"].get(identifier), ours.get(identifier)
            if mine is None:
                continue  # Not known or not published here: left as it is.
            key = "definitions" if mine["kind"] == "plan" else "revisions"
            if len(theirs[key]) > len(mine[key]) or not same(theirs[key], mine[key][:len(theirs[key])]):
                behind.append(path)
        where = {source_id: tree["paths"][identifier]
                 for identifier, model in [*tree["records"].items(), *tree["plans"].items()] for source_id in model["sources"]}
        for source_id, source in sorted(tree["sources"].items()):
            if any(source_id in model["sources"] and J(model["sources"][source_id]) != J(source) for model in ours.values()):
                behind.append(f"source {source_id} is published differently in {where[source_id]}")
        if behind:
            raise MemoryConflict("The published memory has revisions this store does not have; import it first: "
                                 + ", ".join(behind))


def _writable(model):
    """Hold, before anything refers to it, what the format cannot hold: a file over 1 MiB, or one that does not
    read back as written (a scope, link or name the reader refuses). A held record never blocks the others."""
    from .memory_published_parse import parse_file
    data = render(model)
    if len(data) > MAX_FILE_BYTES:
        raise Held("its file would be larger than 1 MiB")
    try:
        parse_file(data)
    except ValueError as exc:
        raise Held(f"it cannot be written in the published format: {exc}") from None


def _listing(errors):
    return "; ".join(f"{path or '(tree)'}: {reason}" for path, reason in errors[:50]) + (
        f"; and {len(errors) - 50} more" if len(errors) > 50 else "")


def _held(held):
    items = [{"id": identifier, "kind": kind, "reason": reason} for identifier, (kind, reason) in sorted(held.items())]
    reasons = {}
    for item in items:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    return {"total": len(items), "by_reason": reasons, "items": items[:200]}


def publication_status(store, project_id, worktree=None):
    """`memory status` → published: where the package is, whose it is and how many files it has. Computed live."""
    project, _, folder = Publisher(store).destination(project_id, worktree)
    files, errors = read_tree(folder) if folder.exists() else ({}, [])
    identity = None
    if "FORMAT.json" in files:
        from .memory import _parse
        try:
            identity = _parse(files["FORMAT.json"]).get("project_id")
        except (ValueError, AttributeError):
            identity = None
    return {"destination": str(folder), "format_exists": "FORMAT.json" in files, "format_project_id": identity,
            "publication_id": one_row(store, "SELECT publication_id FROM projects WHERE id=:id",
                                      {"id": project_id}).get("publication_id"),
            "files": len(files), "unreadable": [path for path, _ in errors]}
