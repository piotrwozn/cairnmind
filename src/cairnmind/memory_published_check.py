"""Checking a whole published memory directory without a store (#170): `memory check-published`.

The tree is read from a directory, or from a {path: bytes} map (a Git tree read with `git cat-file`,
#172). Any failure refuses the whole tree and names every bad path. The checks that need more than one
file come after every file parsed: one reference per source, one active record per kind and key, plan
steps that agree with their task files, and the structural checks of the archive (memory._validate,
without its graph), which also catch dependency cycles across files.
"""
from __future__ import annotations

from pathlib import Path

from .memory_published_format import (
    FORMAT, GITATTRIBUTES, GITIGNORE, MAX_FILE_BYTES, MAX_FILES, MAX_PACKAGE_BYTES, RECORD_KINDS, VERSION, J,
    PublishedFormatError, canonical_uuid, conflict_marker, decode, path_of, ref_class, sha256_text,
)
from .memory_published_parse import parse_file

STATIC_FILES = ("FORMAT.json", ".gitattributes", ".gitignore")
DIRECTORIES = (*RECORD_KINDS, "plan")


def format_bytes(project_id) -> bytes:
    return (J({"format": FORMAT, "format_version": VERSION, "project_id": project_id}) + "\n").encode()


def read_tree(directory):
    """The files of a published directory as {relative path: bytes}, and the entries it refuses."""
    from .memory import _read
    directory, files, errors, total = Path(directory), {}, [], 0
    if directory.is_symlink() or not directory.is_dir():
        return files, [(str(directory), "not a directory (or a symbolic link)")]
    for entry in sorted(directory.iterdir()):
        name = entry.name
        if entry.is_symlink():
            errors.append((name, "symbolic links are not allowed"))
        elif entry.is_file() and name in STATIC_FILES:
            files[name] = _read(entry, MAX_FILE_BYTES)
        elif entry.is_dir() and name in DIRECTORIES:
            for child in sorted(entry.iterdir()):
                relative = f"{name}/{child.name}"
                if child.is_symlink() or child.is_dir():
                    errors.append((relative, "only files are allowed here, no links or directories"))
                elif not (child.name.endswith(".md") and canonical_uuid(child.name[:-3])):
                    errors.append((relative, "unknown entry"))
                elif child.stat().st_size > MAX_FILE_BYTES:
                    errors.append((relative, "the file is larger than 1 MiB"))
                else:
                    files[relative] = _read(child, MAX_FILE_BYTES)
        else:
            errors.append((name, "unknown entry"))
        total = sum(len(data) for data in files.values())
        if len(files) > MAX_FILES or total > MAX_PACKAGE_BYTES:
            return files, [("", "the package exceeds 100,000 files or 256 MiB")]
    return files, errors


def scan(models):
    """The secrets gate (#171) hooks in here: [{path, field, kind}] for every value that must not travel.

    Called on what is about to be written and on what a check reads, before anything else is done with
    it. Version 1 registers nothing yet, which is why publishing stays switched off outside tests.
    """
    return []


def record_row(project_id, model, revision, version):
    """A published revision as an archive record row (task fields from the revision, nothing derived)."""
    metadata = {}
    if model["kind"] == "task":
        metadata = {name: revision[name] for name in ("state", "depends_on", "requires_test", "requires_review",
                                                      "required_profiles", "acceptance_criteria")}
        if revision["criterion_checks"]:
            metadata["criterion_checks"] = revision["criterion_checks"]  # Stored only when there is a link.
        for name, stored in (("plan", "plan_ref"), ("step_key", "step_key"), ("scope", "scope")):
            if revision[name] is not None:
                metadata[stored] = revision[name]
    if revision["quote"] is not None:
        metadata["evidence"] = revision["quote"]["text"]
    return {"id": model["id"], "project_id": project_id, "kind": model["kind"], "key": revision["key"],
            "content": revision["text"], "status": revision["status"], "source_ids": list(revision["sources"]),
            "version": version, "supersedes": revision["supersedes"], "metadata": metadata,
            "created_at": model["created_at"], "updated_at": revision["at"]}


def stub_row(project_id, source_id, source):
    content = source["passage"] if source["passage"] is not None else ""
    return {"id": source_id, "project_id": project_id, "uri": "published:" + source_id, "kind": "published",
            "content": content, "sha256": sha256_text(content),
            "metadata": {"origin": "publication_review" if ref_class(source["ref"]) == "review" else "publication_import",
                         "published": source["ref"], "published_quotes": []},
            "is_current": False, "current_key": None, "created_at": source["ref"]["at"]}


def _cross_file(records, plans, paths):
    errors, sources, keys = [], {}, {}
    for model in [*records.values(), *plans.values()]:
        for source_id, source in model["sources"].items():
            seen = sources.setdefault(source_id, (source, paths[model["id"]]))
            if J(seen[0]) != J(source):
                errors.append((paths[model["id"]], f"source {source_id} is published differently in {seen[1]}"))
    for model in records.values():
        head = model["revisions"][-1]
        if head["status"] != "superseded":
            keys.setdefault((model["kind"], head["key"]), []).append(paths[model["id"]])
        replaced = records.get(head["supersedes"]) if head["supersedes"] else None
        if head["supersedes"] and head["status"] != "superseded" and (
                replaced is None or replaced["revisions"][-1]["status"] != "superseded"):
            errors.append((paths[model["id"]], f"it supersedes {head['supersedes']}, which is not superseded here"))
        if model["kind"] == "task" and head["plan"] and head["status"] != "superseded":
            plan = plans.get(head["plan"])
            if plan is None or model["id"] not in [s[0] for s in plan["definitions"][-1]["steps"]]:
                errors.append((paths[model["id"]], f"its plan {head['plan']} does not list it as a step"))
    for (kind, key), found in keys.items():
        if len(found) > 1:
            errors += [(path, f"two active {kind} records share the key {key!r}: {', '.join(found)}") for path in found]
    for plan in plans.values():
        steps = plan["definitions"][-1]["steps"]
        if len({s[1] for s in steps}) != len(steps):
            errors.append((paths[plan["id"]], "two steps share a step key"))
        for task_id, step_key in steps:
            task = records.get(task_id)
            head = task["revisions"][-1] if task and task["kind"] == "task" else None
            if head is None or head["plan"] != plan["id"] or head["step_key"] != step_key:
                errors.append((paths[plan["id"]], f"step {task_id} does not match task/{task_id}.md"))
    return errors, {source_id: value[0] for source_id, value in sources.items()}


def payload_of(project_id, records, plans, sources):
    """The tree in the archive's shape, for memory._validate and for show-published."""
    payload = {table: [] for table in ("sources", "records", "record_revisions", "plans", "plan_revisions",
                                       "nodes", "edges", "check_runs")}
    payload["sources"] = [stub_row(project_id, source_id, source) for source_id, source in sorted(sources.items())]
    order = {task_id: index for plan in plans.values() for index, (task_id, _) in enumerate(plan["definitions"][-1]["steps"])}
    heads = {}
    for model in records.values():
        for number, revision in enumerate(model["revisions"], 1):
            row = record_row(project_id, model, revision, number)
            if number == len(model["revisions"]):
                if model["id"] in order and row["metadata"].get("plan_ref"):
                    row["metadata"]["step_index"] = order[model["id"]]
                heads[model["id"]] = row
                payload["records"].append(row)
            payload["record_revisions"].append({"id": f"{model['id']}:{number}", "project_id": project_id,
                                                "record_id": model["id"], "version": number, "snapshot": row})
    for model in plans.values():
        last = model["definitions"][-1]
        plan = {"id": model["id"], "project_id": project_id, "version": 1, "title": last["title"], "goal": last["goal"],
                "constraints": last["constraints"], "source_ids": list(last["sources"]),
                "step_ids": [step[0] for step in last["steps"]], "created_at": model["created_at"], "updated_at": last["at"]}
        payload["plans"].append(plan)
        payload["plan_revisions"].append({"id": f"{model['id']}:1", "project_id": project_id, "plan_id": model["id"],
                                          "version": 1, "snapshot": {"plan": plan, "tasks": [
                                              heads[i] for i in plan["step_ids"] if i in heads]}})
    return payload


def check_files(files):
    """Phase 1 of an import: (tree, errors). The tree is None whenever there is an error."""
    from .memory import _parse, _validate
    errors = []
    for path, raw in sorted(files.items()):
        try:
            marker = conflict_marker(decode(raw))
        except PublishedFormatError:
            marker = None
        if marker is not None:
            errors.append((path, f"unresolved conflict in {path} line {marker}"))
    if errors:
        return None, errors
    project_id = None
    if "FORMAT.json" not in files:
        errors.append(("FORMAT.json", "missing"))
    else:
        try:
            value = _parse(decode(files["FORMAT.json"]).encode())
            project_id = value.get("project_id") if isinstance(value, dict) else None
            if not canonical_uuid(project_id) or decode(files["FORMAT.json"]).encode() != format_bytes(project_id):
                raise ValueError("not this format")
        except (ValueError, PublishedFormatError, AttributeError):
            errors.append(("FORMAT.json", f"not {FORMAT} version {VERSION}"))
    for name, expected in ((".gitattributes", GITATTRIBUTES), (".gitignore", GITIGNORE)):
        if name in files and decode(files[name]).encode() != expected:
            errors.append((name, "differs from what the format writes"))
    records, plans, paths = {}, {}, {}
    for path, raw in sorted(files.items()):
        if path in STATIC_FILES:
            continue
        try:
            model = parse_file(raw)
        except (PublishedFormatError, ValueError) as exc:
            errors.append((path, str(exc)))
            continue
        if path != path_of(model):
            errors.append((path, f"{path} holds {model['kind']} {model['id']}; expected {path_of(model)}"))
            continue
        (plans if model["kind"] == "plan" else records)[model["id"]] = model
        paths[model["id"]] = path
    if errors:
        return None, errors
    cross, sources = _cross_file(records, plans, paths)
    if cross:
        return None, cross
    payload = payload_of(project_id, records, plans, sources)
    try:
        _validate(payload, project_id, graph=False)
    except ValueError as exc:
        return None, [("", f"the tree as a whole is invalid: {exc}")]
    hits = scan({"records": records, "plans": plans, "paths": paths})
    if hits:
        return None, [(hit["path"], f"{hit['kind']} found in {hit['field']}; nothing was imported") for hit in hits]
    return {"project_id": project_id, "records": records, "plans": plans, "paths": paths, "sources": sources,
            "payload": payload}, []


def check_directory(directory):
    files, errors = read_tree(directory)
    if errors:
        return None, errors
    return check_files(files)
