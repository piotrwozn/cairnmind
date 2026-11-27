"""Importing the published memory of a commit and re-checking it against that commit's code (#172).

`memory import-commit REVISION --project P` is what a CI job or a server runs after a merge. It reads
the published directory of the commit from Git (nothing is checked out), imports it like
`import-published`, and in the same transaction compares every imported file source with the
project's files at that commit: whether the file is still at its path, whether its content is the one
published, and whether each passage a record quotes from it is still there. The result is kept on
the source stub (`rechecked`) and read by evidence.py: a passage still there anchors the record, one
that is gone (or a file that is gone) leaves it orphaned. Anchoring is a local fact about this commit,
never verification: nothing another machine said is verified here.

A record imported earlier whose file is no longer in the commit is reported as withdrawn; laptop and
main-branch imports alike never delete or change it.
"""
from __future__ import annotations

import hashlib

from cairnmind.memory.memory_published_format import RECORD_KINDS, covered_quotes, ref_class, sha256_text
from cairnmind.memory.memory_published_git import MAX_CHECKED_BYTES, project_files, published_files, read_blobs
from cairnmind.storage.sql import clean_row
from cairnmind.storage.storage_query import db_find, db_update_where, eq, is_in
from cairnmind.pal.terminal_safe import safe_text


def _rows(tx, collection, field, identifiers, *, where=(), columns=None):
    identifiers, rows = sorted(identifiers), []
    for start in range(0, len(identifiers), 500):
        filters = [*where, is_in(field, identifiers[start:start + 500])]
        rows += [clean_row(row) for row in db_find(tx, collection, filters, columns=columns)]
    return rows


def recheck(tx, project_id, tree, root, commit, nested=()):
    """{source ID: result} for every imported file stub whose result changed; writes those results.

    `nested` are the reserved directories of projects registered below the root (#174), left out like its own."""
    targets = {source_id: source for source_id, source in tree["sources"].items() if ref_class(source["ref"]) == "file"}
    stubs = {
        row["id"]: row
        for row in _rows(tx, "sources", "id", targets, columns=("id", "project_id", "kind", "metadata"))
        if row.get("project_id") == project_id
    }
    # A full local source with the same ID is this machine's own evidence, judged by its own indexing.
    targets = {source_id: source for source_id, source in targets.items()
               if stubs.get(source_id, {}).get("kind") == "published"}
    if not targets:
        return {}, 0
    quotes = {}
    for model in tree["records"].values():
        # Only a quote a person's act hashed is compared: any other quote can be edited in the file freely,
        # so its record is judged by its file alone.
        for source_id, text in covered_quotes(model):
            if source_id in targets:
                quotes.setdefault(source_id, set()).add(text)
    files = project_files(root, commit, nested)
    found = {source_id: files.get(source["ref"]["path"]) for source_id, source in targets.items()}
    blobs = read_blobs(root, {entry[0] for entry in found.values() if entry and (entry[1] or 0) <= MAX_CHECKED_BYTES})
    changed, unchecked = {}, 0
    for source_id, source in sorted(targets.items()):
        entry, texts = found[source_id], sorted(quotes.get(source_id, ()))
        if entry is None:
            value = {"file": "gone", "quotes": {sha256_text(text): False for text in texts}}
        elif entry[0] not in blobs:
            unchecked += 1  # Too large to compare: left as it was, unverified.
            continue
        else:
            data = blobs[entry[0]]
            content = data.decode("utf-8", "replace").replace("\r\n", "\n")
            value = {"file": "same" if hashlib.sha256(data).hexdigest() == source["ref"]["sha256"] else "changed",
                     "quotes": {sha256_text(text): text in content for text in texts}}
        value["path"] = source["ref"]["path"]
        metadata = stubs[source_id].get("metadata") or {}
        earlier = metadata.get("rechecked")
        if isinstance(earlier, dict) and {k: v for k, v in earlier.items() if k != "commit"} == value:
            continue  # The same result: the commit it was first found at stays, and nothing is written.
        value["commit"] = commit
        db_update_where(tx, "sources", set_fields={"metadata": {**metadata, "rechecked": value}}, where={"id": source_id})
        changed[source_id] = value
    return changed, unchecked


def _title(record):
    text = " ".join(safe_text(str(record.get("content", "")).strip().split("\n", 1)[0]).split())
    return text if len(text) <= 100 else text[:99] + "…"


def _claimed(record):
    """Who the file says acted last, as the file says it: "confirm by Ada Nowak on main (claimed by the file)"."""
    entries = ((record.get("metadata") or {}).get("publication") or {}).get("reviewed_by") or []
    if not entries or not isinstance(entries[-1], dict):
        return None
    last = entries[-1]
    where = f" on {last['branch']}" if last.get("branch") else ""
    return safe_text(f"{last.get('action')} by {last.get('name')}{where} (claimed by the file; not verified here)")


def import_commit(store, project_id, revision, *, worktree=None, dry_run=False, bind=False, authorize=None,
                  replace_confirmed=False, expected_replaced=None):
    from cairnmind.memory.evidence import evidence_states
    from cairnmind.memory.memory import MemoryConflict
    from cairnmind.memory.memory import MemoryManager
    from cairnmind.memory.memory_paths import nested_reserved
    from cairnmind.memory.memory_published import _listing
    from cairnmind.memory.memory_published_carrier import carrier_of
    from cairnmind.memory.memory_published_import import PublishedImporter
    project, root = MemoryManager(store)._scope(project_id, worktree)
    carrier = carrier_of(store, project_id)
    if carrier["kind"] != "repository":
        # The checkout carrier (#173) keeps the package in a separate repository, whose commits hold no code to
        # re-check passages against: a commit of the project's repository does not hold this project's memory.
        identity = project.get("publication_id") or project_id
        raise ValueError(f"The published memory of this project goes to a checkout of a separate repository "
                         f"({safe_text(str(carrier.get('path')))}), not to its own repository, so a commit of the "
                         f"project holds none of it. Import that package with `cairnmind memory import-published "
                         f"{safe_text(str(carrier.get('path')))}/{identity} --project {safe_text(project['name'])}`.")
    # The memory directories of projects registered below this one (#174) are not its code.
    nested = nested_reserved(store, root)
    commit, files, errors = published_files(root, revision)
    if errors:
        raise MemoryConflict(f"The published memory of commit {commit[:12]} was refused: " + _listing(errors))
    base = {"commit": commit, "verification": "nothing imported is verified here; a passage found in this commit "
                                              "anchors a record, it does not verify it", "model_calls": 0}
    if not files:
        return {"status": "nothing_published", **base, "rechecked": [], "withdrawn": [], "evidence": {}}
    seen = {}

    def after(tx, tree, result):
        seen["tree"] = tree
        seen["changed"], seen["unchecked"] = recheck(tx, project_id, tree, root, commit, nested)

    # The secrets gate (#171): the commit's own personal-data patterns, as CI read them on the pull request,
    # and this machine's registered secrets.
    from cairnmind.memory.memory_published_release import SecretsRefused, commit_config_text, import_rules
    from cairnmind.memory.memory_published_secrets import SecretRulesError
    try:
        config = commit_config_text(root, commit)
    except SecretRulesError as exc:
        raise SecretsRefused(f"Import refused: {exc}") from None
    rules = import_rules(None, store.settings, config=config)
    result = PublishedImporter(store).import_published(files, project_id, dry_run=dry_run, bind=bind,
                                                       authorize=authorize, after=after, rules=rules,
                                                       replace_confirmed=replace_confirmed,
                                                       expected_replaced=expected_replaced)
    tree = seen["tree"]
    result.update(base)
    result["rechecked"] = sorted(seen["changed"])
    result["unchecked"] = seen["unchecked"]
    rows = _rows(store, "records", "id", tree["records"], where=[eq("project_id", project_id)])
    every = [
        clean_row(row)
        for row in db_find(
            store,
            "records",
            [eq("project_id", project_id), is_in("kind", list(RECORD_KINDS))],
            columns=("id", "kind", "status", "content", "metadata"),
        )
    ]
    withdrawn = [row for row in every if row["id"] not in tree["records"] and row.get("status") != "superseded"
                 and isinstance((row.get("metadata") or {}).get("publication"), dict)]
    result["withdrawn"] = [{"id": row["id"], "kind": row["kind"], "path": row["metadata"]["publication"].get("path"),
                            "title": _title(row)} for row in sorted(withdrawn, key=lambda r: r["id"])]
    if dry_run:
        result["evidence"] = {"note": "a dry run computes no evidence states"}
        return result
    states = evidence_states(store, project_id, [row for row in rows if row.get("status") != "superseded"])
    evidence = {}
    for row in rows:
        if row["id"] in states:
            evidence.setdefault(states[row["id"]]["evidence_state"], []).append(
                {"id": row["id"], "kind": row["kind"], "path": tree["paths"][row["id"]], "title": _title(row),
                 "reviewed": _claimed(row)})
    result["evidence"] = {state: sorted(items, key=lambda item: item["path"]) for state, items in evidence.items()}
    return result
