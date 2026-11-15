"""Importing a merged published memory directory into a local project (#170).

Phase 1 is the store-free check of the whole tree. Phase 2 runs in one store transaction, all or
nothing. A record unknown here gets one local version per published revision; a record whose local
head is an earlier published revision is fast-forwarded; a record whose local history already holds the
published head is left alone. Anything else diverged, and the whole import is refused with the paths,
unless `--take-published ID` asks to append the published head as one more local version (nothing is
ever removed). Sources arrive as stubs of kind "published": their passage or nothing, never a file's
content. Nothing imported is verified, and no review is written as this machine's own.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from .memory_paths import PUBLISHED_RELATIVE
from .memory_published import version_projection
from .memory_published_check import check_directory, check_files, record_row, stub_row
from .memory_published_format import J, canonical_uuid, projection, reviewed_by, same, sha256_text
from .sql import clean_row, evidence_time, insert_document, json_text, one_row, update_document, utc_now


class _DryRun(Exception):
    def __init__(self, result):
        super().__init__("dry run")
        self.result = result


def _git_top(root):
    """The top of a root's Git worktree; None outside Git. A Git that does not answer refuses the import."""
    from .verification import _git
    try:
        return Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip()).resolve()
    except (OSError, ValueError):
        return None
    except subprocess.SubprocessError as exc:
        from .memory import MemoryConflict
        raise MemoryConflict(f"Git did not answer for {root}, so it cannot be told whether the directory "
                             "is another project's; nothing was imported") from exc


def _own_directory(tx, directory, project_id):
    """Refuse another project's published directory (#174).

    One repository can hold several projects, each publishing at its own root. A directory named
    `.memory/cairnmind-published` belongs to the directory that holds it. It is refused when that one
    is a root of another local project, or lies in the Git worktree of this project's root without
    being that root: then it is another project's in the same repository, registered here or not.
    Anything else (a teammate's checkout, a copy, a directory of another name) is decided by its
    FORMAT.json and the binding alone: this is a guard against mistakes inside a repository, not a
    proof of where a directory came from. The path is resolved first, parents included, and its name
    compared without case, so a symbolic link or a differently cased name does not lead around it.
    """
    if not isinstance(directory, (str, os.PathLike)):
        return
    path = Path(directory).resolve()
    tail = tuple(part.casefold() for part in path.parts[-len(PUBLISHED_RELATIVE.parts):])
    if tail != tuple(part.casefold() for part in PUBLISHED_RELATIVE.parts):
        return
    holder = path.parents[len(PUBLISHED_RELATIVE.parts) - 1]
    roots = {Path(row["path"]).resolve() for row in tx.query(
        "SELECT path FROM project_roots WHERE project_id=:project", {"project": project_id})}
    if holder in roots:
        return
    others = {Path(row["path"]).resolve() for row in tx.query(
        "SELECT path FROM project_roots WHERE project_id<>:project", {"project": project_id})}
    tops = {top for top in map(_git_top, sorted(roots)) if top is not None}
    if holder in others or any(holder.is_relative_to(top) for top in tops):
        from .memory import MemoryConflict
        raise MemoryConflict(f"{path} is the published memory of {holder}, not of this project's root; "
                             "each project imports only its own directory")


def _core(definition):
    return {key: definition[key] for key in ("title", "goal", "constraints", "steps")}


def _attached(model, path, numbers, versions, sources):
    """Local sources a file would newly attach to a record: refused, since they would stand as evidence here.

    A source this machine holds in full (not a published stub) may be cited by a written revision only when
    a local version of the same record already cited it, or when it is a local review audit (never evidence)
    or the source of the plan the task belongs to. Otherwise a hand-edited file could copy a source ID from
    any file this machine published and make a record nobody here judged anchored.
    """
    before = {s for version in versions for s in version.get("source_ids") or []}
    found = []
    for index in numbers:
        revision = model["revisions"][index]
        for source_id in revision["sources"]:
            row = sources.get(source_id)
            if row is None or row["kind"] == "published" or source_id in before:
                continue
            metadata, uri = row.get("metadata") or {}, str(row.get("uri", ""))
            if (uri.startswith("review://") and metadata.get("origin") == "local_user_review") or (
                    row["kind"] == "plan" and revision.get("plan") and uri == f"plan://{revision['plan']}"):
                continue
            found.append(f"{path} cites source {source_id}, which this record never cited here")
    return sorted(set(found))


class PublishedImporter:
    def __init__(self, store):
        self.store = store

    def import_published(self, directory, project_id, *, dry_run=False, bind=False, take_published=(), authorize=None,
                         after=None, rules=None, replace_confirmed=False):
        """`directory` is a path or a {relative path: bytes} map (a commit read from Git, #172). `after(tx, tree,
        result)` runs in the same transaction once the tree is applied, before a dry run is rolled back.

        The secrets gate (#171) holds for both: `rules` default to the checkout's personal patterns (none for
        a map) and this machine's registered secrets; the import of a commit passes the commit's patterns."""
        from .memory import MemoryConflict
        from .memory_published import _listing
        from .memory_published_release import import_rules
        if rules is None:
            rules = import_rules(None if isinstance(directory, dict) else directory, self.store.settings)
        tree, errors = check_files(directory, rules) if isinstance(directory, dict) else check_directory(directory, rules)
        if errors:
            raise MemoryConflict("The published memory was refused: " + _listing(errors))
        take = set(take_published or ())
        if any(not canonical_uuid(i) or i not in tree["records"] for i in take):
            # Plans are not taken: a plan's local history has no mark to follow the published one (see _plans).
            raise ValueError("--take-published names a record that is not in the published memory")
        result = {"status": "dry_run" if dry_run else "imported", "project_id": project_id,
                  "publication_id": tree["project_id"], "inserted": [], "advanced": [], "unchanged": 0,
                  "taken": [], "stubs": 0, "plans": [], "verification": "nothing imported is verified here",
                  "model_calls": 0}
        try:
            with self.store.transaction() as tx:
                if authorize is not None:
                    authorize()
                # A map read from a commit (#172) is the project's own directory by construction: it is read at
                # the project root's prefix, so _own_directory (#174) only judges a path.
                self._apply(tx, tree, project_id, bind, take, result, directory)
                replaced = result["replaces_confirmed_here"]["records"]
                if replaced and not replace_confirmed and not dry_run:
                    # A verdict a person gave here is never replaced by a file without that person's choice (#177).
                    from .confirmation import replaces_refusal
                    raise MemoryConflict(replaces_refusal(replaced))
                if after is not None:
                    after(tx, tree, result)
                if dry_run:
                    raise _DryRun(result)
        except _DryRun as done:
            return done.result
        return result

    def _apply(self, tx, tree, project_id, bind, take, result, directory=None):
        from .memory import MemoryConflict
        project = one_row(tx, "SELECT FROM projects WHERE id=:id", {"id": project_id})
        # Another project's directory first (#239): no binding or ID check could make it this project's.
        _own_directory(tx, directory, project_id)
        identity = project.get("publication_id")
        if identity not in (None, tree["project_id"]) and not bind:
            raise MemoryConflict(f"This project publishes as {identity}, but the directory belongs to "
                                 f"{tree['project_id']}; pass --bind to replace the binding")
        for table, identifiers in (("records", list(tree["records"])), ("plans", list(tree["plans"])),
                                   ("sources", list(tree["sources"]))):
            for start in range(0, len(identifiers), 500):
                if any(row.get("project_id") != project_id for row in tx.query(
                        f"SELECT project_id FROM {table} WHERE id IN :ids", {"ids": identifiers[start:start + 500]})):
                    raise MemoryConflict("A memory entity ID belongs to another local project")
        if identity != tree["project_id"]:
            update_document(tx, "projects", {"publication_id": tree["project_id"]}, "id=:id", {"id": project_id})
        self._stubs(tx, tree, project_id, result)
        plans = {i: m for i, m in tree["plans"].items()}
        written, diverged, foreign = {}, [], []
        heads, kept, replaced = {}, {}, []
        from .confirmation import MARK, local_verdict, said

        def statement(model, index):
            row = record_row(project_id, model, model["revisions"][index], 0)
            return said(row["kind"], row["content"], row["key"], row["supersedes"], row["metadata"])
        for record_id, model in sorted(tree["records"].items()):
            path = tree["paths"][record_id]
            versions = [clean_row(r)["snapshot"] for r in tx.query(
                "SELECT snapshot,version FROM record_revisions WHERE record_id=:id ORDER BY version", {"id": record_id})]
            if versions and versions[-1]["kind"] != model["kind"]:
                raise MemoryConflict(f"{path}: a local record with this ID has another kind")
            sources = self._sources(tx, project_id, {s for v in versions for s in v.get("source_ids") or []}
                                    | set(model["sources"]))
            local = [version_projection(v, sources) for v in versions]
            published = [projection(r) for r in model["revisions"]]
            if not versions:
                plan, start = list(range(len(published))), "inserted"
            elif any(same(published[-1], value) for value in local):
                result["unchanged"] += 1
                heads[record_id] = versions[-1]
                continue
            else:
                matches = [k for k, value in enumerate(published) if same(value, local[-1])]
                if matches:
                    plan, start = list(range(matches[-1] + 1, len(published))), "advanced"
                    head, last = versions[-1], model["revisions"][-1]
                    if local_verdict(head):
                        # The files move on a record that was confirmed or disputed here (#177). When they leave
                        # it confirmed and saying the same (progress on a task), the confirmation given here is
                        # kept. Anything else replaces that verdict, which only a person chooses.
                        if (head["status"] == "confirmed" == last["status"]
                                and statement(model, matches[-1]) == statement(model, len(published) - 1)):
                            kept[record_id] = (head.get("metadata") or {}).get(MARK)
                        else:
                            replaced.append(path)
                elif record_id in take:
                    # Every published revision after the part both histories share, as new local versions: the
                    # local ones stay, and the next publication follows the published history from there.
                    mine = self._published_here(project_id, model["kind"], record_id, versions, sources)
                    common = 0
                    while common < min(len(mine), len(published)) and same(mine[common], model["revisions"][common]):
                        common += 1
                    plan, start = list(range(common, len(published))) or [len(published) - 1], "taken"
                else:
                    diverged.append(path)
                    continue
            written[record_id] = (model, path, versions[-1] if versions else None, plan)
            result[start].append(path)
            foreign += _attached(model, path, plan, versions, sources)
        if foreign:
            raise MemoryConflict("A file cannot attach this machine's own sources to a record: " + "; ".join(foreign))
        if diverged:
            raise MemoryConflict("These records diverged from the published memory; re-apply your change after "
                                 "--take-published, or resolve the conflict by taking one side: " + ", ".join(diverged))
        from .confirmation import REPLACES_NOTE
        result["replaces_confirmed_here"] = {"total": len(replaced), "records": sorted(replaced), "note": REPLACES_NOTE}
        # What the files bring as confirmed (#177): kept as confirmed elsewhere, with who each file says
        # reviewed it; the preview and the result both say how many, and that none is this machine's own.
        brought = [model for record_id, (model, _, _, numbers) in written.items()
                   if numbers and model["revisions"][numbers[-1]]["status"] == "confirmed" and record_id not in kept]
        claimed = [(reviewed_by(model, model["revisions"]) or [{}])[-1].get("name") for model in brought]
        result["confirmed_records"] = {
            "total": len(brought), "confirmed_by": sorted({str(name) for name in claimed if name})[:50],
            "note": "confirmed elsewhere (claimed by the files); kept as confirmed, shown as imported, "
                    "never verified here"}
        for record_id, (_, _, old, _) in written.items():
            if old is not None:
                update_document(tx, "records", {"active_key": None}, "id=:id", {"id": record_id})
        now = utc_now()
        for record_id, (model, path, old, numbers) in written.items():
            version = old["version"] if old else 0
            for index in numbers:
                version += 1
                heads[record_id] = self._write(tx, project_id, model, index, version, path, plans, now,
                                               keep=(kept[record_id],) if record_id in kept and index == numbers[-1]
                                               else None)
        touched_plans = self._plans(tx, project_id, tree, written, heads, result, now)
        for task_id, model in tree["records"].items():
            head = heads.get(task_id)
            if model["kind"] != "task" or head is None or head["metadata"].get("plan_ref") not in touched_plans:
                continue
            wanted = self._placement(plans, head["metadata"]["plan_ref"], task_id)
            if any(head["metadata"].get(k) != v for k, v in wanted.items()):
                row = {**head, "metadata": {**head["metadata"], **wanted}, "version": head["version"] + 1, "updated_at": now}
                heads[task_id] = self._store(tx, row, tree["paths"][task_id])
                written.setdefault(task_id, None)
        for _ in range(2):  # The second pass adds dependency edges to records written after their dependants.
            for record_id in written:
                self.store._record_graph(tx, heads[record_id])
        from .plans import _plan_graph, _tasks
        for plan_id in touched_plans:
            row = clean_row(one_row(tx, "SELECT FROM plans WHERE id=:id", {"id": plan_id}))
            _plan_graph(self.store, tx, row, _tasks(tx, project_id, row))
        if written or touched_plans or result["stubs"]:
            from .journal import Journal
            Journal.reset(tx, reason="publication_import")

    def _stubs(self, tx, tree, project_id, result):
        from .memory import MemoryConflict
        quotes = {}
        for model in tree["records"].values():
            for revision in model["revisions"]:
                if revision["quote"] is not None:
                    entry = {"lines": revision["quote"]["lines"], "sha256": sha256_text(revision["quote"]["text"])}
                    if entry not in quotes.setdefault(revision["quote"]["source"], []):
                        quotes[revision["quote"]["source"]].append(entry)
        for source_id, source in sorted(tree["sources"].items()):
            stub = stub_row(project_id, source_id, source)
            stub["metadata"]["published_quotes"] = sorted(quotes.get(source_id, []), key=J)
            existing = one_row(tx, "SELECT FROM sources WHERE id=:id", {"id": source_id}, False)
            if existing is None:
                insert_document(tx, "sources", stub)
                result["stubs"] += 1
            elif existing["kind"] == "published":
                metadata = existing.get("metadata") or {}
                if J(metadata.get("published")) != J(source["ref"]) or existing["content"] != stub["content"]:
                    raise MemoryConflict(f"Source {source_id} is published differently from the one imported earlier")
                known = list(metadata.get("published_quotes") or [])
                grown = sorted(known + [q for q in stub["metadata"]["published_quotes"] if q not in known], key=J)
                if grown != known:
                    update_document(tx, "sources", {"metadata": {**metadata, "published_quotes": grown}},
                                    "id=:id", {"id": source_id})
            # A full local source with the same ID is this machine's own evidence: kept untouched.

    def _published_here(self, project_id, kind, record_id, versions, sources):
        """What this store would publish for the record now; empty when it would hold it."""
        from .memory_published import Held, Publisher, select_history, source_ref
        root = Publisher(self.store).destination(project_id)[1]

        def as_source(source_id):
            ref, passage = source_ref(sources[source_id], root)
            return {"ref": ref, "passage": passage}
        try:
            return select_history(kind, record_id, versions, sources, as_source)
        except Held:
            return []

    @staticmethod
    def _sources(tx, project_id, identifiers):
        rows = {}
        identifiers = sorted(identifiers)
        for start in range(0, len(identifiers), 500):
            for row in tx.query("SELECT id,project_id,uri,kind,sha256,metadata,created_at,content FROM sources "
                                "WHERE id IN :ids", {"ids": identifiers[start:start + 500]}):
                if row.get("project_id") == project_id:
                    rows[row["id"]] = clean_row(row)
        return rows

    @staticmethod
    def _placement(plans, plan_id, task_id):
        from .plans import _digest
        plan = plans.get(plan_id)
        if plan is None:
            return {}
        last = plan["definitions"][-1]
        steps = [step[0] for step in last["steps"]]
        wanted = {"plan_context_hash": _digest({"title": last["title"], "goal": last["goal"],
                                                "constraints": last["constraints"]}),
                  "plan_archived": task_id not in steps}
        if task_id in steps:
            wanted["step_index"] = steps.index(task_id)
        return wanted

    def _write(self, tx, project_id, model, index, version, path, plans, now, keep=None):
        revision = model["revisions"][index]
        row = record_row(project_id, model, revision, version)
        metadata = row["metadata"]
        if model["kind"] == "task" and revision["plan"]:
            metadata.update(self._placement(plans, revision["plan"], model["id"]))
        evidence = evidence_time(tx, row["source_ids"])
        metadata.update(evidence_earliest=evidence["earliest"], evidence_latest=evidence["latest"],
                        evidence_basis=evidence["basis"])
        # "version" names the local version this is: a later local edit keeps the mark (it is derived metadata)
        # but not as its own, so only this version stands for published revision `revision`.
        reviews = [e for e in reviewed_by(model, model["revisions"]) if e["revision"] <= index + 1]
        metadata["publication"] = {"path": path, "revision": index + 1, "imported_at": now, "version": version,
                                   "reviewed_by": reviews}
        metadata.pop("confirmation", None)
        if keep is not None:
            # The confirmation given here stands: the files changed nothing it covers. A record from before
            # the rule had no mark and gets none.
            if isinstance(keep[0], dict):
                metadata["confirmation"] = keep[0]
        elif row["status"] == "confirmed":
            # Confirmed elsewhere (#177): the file's own claim of who reviewed it, never a local act.
            from .confirmation import import_mark
            metadata["confirmation"] = import_mark("published", path, reviews, now, version)
        return self._store(tx, row, path)

    def _store(self, tx, row, path):
        from .memory import MemoryConflict
        row["active_key"] = json_text([row["project_id"], row["kind"], row["key"]]) if row["status"] != "superseded" else None
        if row["active_key"]:
            collision = one_row(tx, "SELECT id FROM records WHERE active_key=:key", {"key": row["active_key"]}, False)
            if collision and collision["id"] != row["id"]:
                raise MemoryConflict(f"{path}: its kind and key belong to the local record {collision['id']}")
        if row["version"] == 1:
            insert_document(tx, "records", row)
        else:
            update_document(tx, "records", row, "id=:id", {"id": row["id"]})
        insert_document(tx, "record_revisions", {"id": f"{row['id']}:{row['version']}", "project_id": row["project_id"],
                                                 "record_id": row["id"], "version": row["version"], "snapshot": row,
                                                 "recorded_at": utc_now()})
        return row

    def _plans(self, tx, project_id, tree, written, heads, result, now):
        from .memory import MemoryConflict
        from .plans import _digest
        touched, diverged = [], []
        for plan_id, model in sorted(tree["plans"].items()):
            path, definitions = tree["paths"][plan_id], model["definitions"]
            local = one_row(tx, "SELECT FROM plans WHERE id=:id", {"id": plan_id}, False)
            steps_written = any(tree["records"][i]["revisions"][-1].get("plan") == plan_id for i in written
                                if i in tree["records"])
            if local is None:
                numbers = list(range(len(definitions)))
            else:
                revisions = [clean_row(r) for r in tx.query("SELECT FROM plan_revisions WHERE plan_id=:id ORDER BY version",
                                                             {"id": plan_id})]
                cores = [{"title": r["snapshot"]["plan"]["title"], "goal": r["snapshot"]["plan"]["goal"],
                          "constraints": r["snapshot"]["plan"]["constraints"],
                          "steps": [[t["id"], t["metadata"].get("step_key")] for t in r["snapshot"]["tasks"]]}
                         for r in revisions]
                published = [_core(d) for d in definitions]
                if any(same(published[-1], core) for core in cores):
                    numbers = []
                else:
                    matches = [k for k, core in enumerate(published) if cores and same(core, cores[-1])]
                    if matches:
                        numbers = list(range(matches[-1] + 1, len(published)))
                    else:
                        # No --take-published for plans in version 1: appending the published definition would
                        # not make this store publish the same definitions, since a plan revision carries no mark.
                        diverged.append(path)
                        continue
                if not numbers and not steps_written:
                    continue
            version = local["version"] if local else 0
            for index in numbers or [None]:
                definition = definitions[index if index is not None else -1]
                current = definitions[-1] if index is None else definition
                version += 1
                row = {"id": plan_id, "project_id": project_id, "version": version, "title": current["title"],
                       "goal": current["goal"], "constraints": current["constraints"],
                       "step_ids": [s[0] for s in current["steps"]] if index is not None else local["step_ids"],
                       "context_hash": _digest({"title": current["title"], "goal": current["goal"],
                                                "constraints": current["constraints"]}),
                       "source_ids": list(current["sources"]) if index is not None else local["source_ids"],
                       "created_at": local["created_at"] if local else model["created_at"],
                       "updated_at": current["at"] if index is not None else now}
                if index is None:
                    row.update({k: local.get(k) for k in ("title", "goal", "constraints", "context_hash")})
                tasks = []
                for task_id in row["step_ids"]:
                    task = heads.get(task_id) or clean_row(one_row(tx, "SELECT FROM records WHERE id=:id", {"id": task_id}))
                    tasks.append(task)
                if local is None and version == 1:
                    insert_document(tx, "plans", row)
                else:
                    update_document(tx, "plans", row, "id=:id", {"id": plan_id})
                insert_document(tx, "plan_revisions", {"id": f"{plan_id}:{version}", "project_id": project_id,
                                                       "plan_id": plan_id, "version": version,
                                                       "reason": "publication_import",
                                                       "snapshot": {"plan": row, "tasks": tasks}, "recorded_at": now})
                local = row
            touched.append(plan_id)
            result["plans"].append(path)
        if diverged:
            raise MemoryConflict("These plans diverged from the published memory; a plan cannot be taken with "
                                 "--take-published, so take the side your store holds in Git: " + ", ".join(diverged))
        return touched
