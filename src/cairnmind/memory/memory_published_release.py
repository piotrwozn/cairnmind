"""What the secrets gate (#171) needs from this machine, and a person's release of one record.

The registered secrets are the ones the agent journal already redacts (the service token and the
ArcadeDB password), the keys saved in the credential store, the values behind the `env:` references of
the saved profiles, and the credential variables that the runtime strips from child environments. They exist
only here, never in CI. Personal-data patterns
come from the project's `.cairnmind/publication.toml`, which CI reads from the same checkout.

A release is a person's act: after reading the record, they let its current hits through with a
reason, in the inspector, with the open workspace's key (person_proof, #306). It is a `release://` source
with origin `local_user_release`, published in the record's file
beside its sources, cited by no revision, and honoured only for the exact values it names.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from cairnmind.memory.memory import MemoryConflict
from cairnmind.memory.memory_published_format import J, canonical_uuid, clean_text, ref_class
from cairnmind.memory.memory_published_secrets import (
    MAX_RELEASE_REASON,
    REGISTERED,
    Rules,
    SecretRulesError,
    described,
    load_rules,
    model_hits,
    release_problem,
    scan_text,
)
from cairnmind.storage.sql import clean_row, utc_now
from cairnmind.storage.storage_query import all_of, any_of, db_find, eq

RELEASE_ORIGIN = "local_user_release"
CONFIG = Path(".cairnmind") / "publication.toml"
# What a person can do about a registered secret in a record (#239). Every version a person confirmed is
# published, so a later text without the value does not take it out of the record's history.
REGISTERED_WHY = ("Every version a person confirmed is published, so changing the text later does not take the value "
                  "out, and the record keeps publishing refused until the value is no longer registered here (for a "
                  "credential: rotate it, then remove the old value from Cairnmind's saved keys and from the "
                  "environment the Cairnmind service runs in).")
CLI_REFUSED = ("The command line no longer releases a record. Nothing was released. The person reads the record in "
               "the inspector of the workspace (`cairnmind`) and chooses Release.")
REGISTERED_ADVICE = "A registered secret is never released. " + REGISTERED_WHY


class SecretsRefused(MemoryConflict):
    """Publishing or a release refused by the secrets gate; the message names files, fields and kinds only."""


def registered_secrets(settings, environ=None):
    """The secrets registered with Cairnmind on this machine (secret_mask.registered_secrets, which ingestion
    shares). Unreadable credentials or profiles refuse, never pass."""
    from cairnmind.memory.secret_mask import registered_secrets as known
    try:
        return known(settings, environ)
    except ValueError as exc:
        raise SecretsRefused(f"The secrets registered here cannot be read, so they cannot be kept out: {exc}") from None


def _read_config(path):
    if path.is_symlink():
        raise SecretRulesError(f"{path.name} must not be a symbolic link")
    if not path.is_file():
        return None
    if path.stat().st_size > 64 * 1024:
        raise SecretRulesError(f"{path.name} is larger than 64 KiB")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise SecretRulesError(f"{path.name} is not UTF-8") from None


def config_text(root):
    return _read_config(Path(root) / CONFIG)


def publication_rules(root, settings) -> Rules:
    try:
        return load_rules(config_text(root), registered_secrets(settings))
    except SecretRulesError as exc:
        raise SecretsRefused(f"Publishing refused: {CONFIG}: {exc}") from None


def checkout_rules(directory, config=None) -> Rules:
    """The rules `check-published DIR` applies: the project's personal patterns, never a registered secret.

    `config` names the file; by default it is `.cairnmind/publication.toml` of the checkout that holds
    DIR at `.memory/cairnmind-published`."""
    directory = Path(directory)
    if config is not None:
        text = _read_config(Path(config))
        if text is None:
            raise SecretRulesError(f"{config} is not a file")
        return load_rules(text)
    if directory.name.casefold() in {"cairnmind-published", "cairnmind", "devbrain"} and directory.parent.name.casefold() == ".memory":
        return load_rules(config_text(directory.parent.parent))
    return Rules()


def import_rules(directory, settings, *, config=None) -> Rules:
    """The rules `memory import-published` applies: the checkout's personal patterns, as CI applies them,
    and this machine's registered secrets, so that nothing this store would refuse to publish comes in.

    A tree read from a commit (#172) has no directory: `directory` is None and `config` is the text of
    the commit's `.cairnmind/publication.toml`, or None when it has none."""
    try:
        base = load_rules(config) if directory is None else checkout_rules(directory)
    except SecretRulesError as exc:
        raise SecretsRefused(f"Import refused: {CONFIG}: {exc}") from None
    return base.with_registered(registered_secrets(settings))


def commit_config_text(root, commit):
    """The text of the project's `.cairnmind/publication.toml` at a commit, held to the rules of the file on
    disk (a regular file, at most 64 KiB, UTF-8); None when the commit has none."""
    from cairnmind.memory.memory_published_git import GitReadError, file_at
    try:
        data = file_at(root, commit, CONFIG.as_posix(), 64 * 1024)
    except GitReadError as exc:
        raise SecretRulesError(f"{CONFIG.name}: {exc}") from None
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretRulesError(f"{CONFIG.name} is not UTF-8") from None


def release_sources(tx, project_id):
    """{record_id: {source_id: row}} of every release this store holds: its own and imported ones."""
    rows = [
        clean_row(row)
        for row in db_find(
            tx,
            "sources",
            [
                eq("project_id", project_id),
                any_of(
                    eq("metadata.origin", RELEASE_ORIGIN),
                    all_of(eq("kind", "published"), eq("metadata.published.kind", "release")),
                ),
            ],
            columns=("id", "project_id", "uri", "kind", "sha256", "metadata", "created_at", "content"),
        )
    ]
    found = {}
    for row in rows:
        metadata = row.get("metadata") or {}
        local = str(row.get("uri", "")).startswith("release://") and metadata.get("origin") == RELEASE_ORIGIN
        if not local and not (row["kind"] == "published" and ref_class(metadata.get("published")) == "release"):
            continue
        record_id = metadata.get("record_id") if local else None
        if not local:
            from cairnmind.memory.memory_published_format import parse_json
            try:
                record_id = parse_json(row["content"]).get("record_id")
            except (ValueError, AttributeError):
                continue
        if canonical_uuid(record_id) and release_problem(row["content"], record_id) is None:
            found.setdefault(record_id, {})[row["id"]] = row
    return found


def release(store, project_id, record_id, reason, worktree=None):
    """Release the current hits of one published record, with a reason that is published with it."""
    from cairnmind.memory.confirmation import ConfirmationRefused
    from cairnmind.memory.memory_acts import act_context
    from cairnmind.memory.memory_published import Publisher
    from cairnmind.terminal.person_proof import PROOF_MISSING, present
    if not canonical_uuid(record_id):
        raise ValueError("RECORD must be a record ID")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_RELEASE_REASON or not clean_text(reason):
        raise ValueError(f"A release needs a reason of at most {MAX_RELEASE_REASON} printable characters")
    reason = reason.strip()
    publisher = Publisher(store)
    built = publisher.build(project_id, worktree)
    model = next((m for m in built["models"] if m["id"] == record_id), None)
    if model is None:
        held = built["held"].get(record_id)
        raise ValueError("The record is not published" + (f": {held[1]}" if held else ""))
    rules = publication_rules(built["root"], store.settings)
    if scan_text(reason, rules):
        raise SecretsRefused("The reason itself holds a secret or personal data; write it without one")
    path = f"{model['kind']}/{record_id}.md"
    hits = model_hits(model, path, rules)
    if any(hit["kind"] == REGISTERED for hit in hits):
        raise SecretsRefused(f"A registered secret is never released ({path}). " + REGISTERED_WHY)
    if not hits:
        raise ValueError("Nothing in this record needs a release")
    released = sorted(({"field": h["field"], "kind": h["kind"], "value_sha256": h["value_sha256"]} for h in hits), key=J)
    audit = {"action": "release", "actor": "local_user", **act_context(store, project_id, worktree), "at": utc_now(),
             "hits": released, "reason": reason, "record_id": record_id}
    passage = J(audit)
    problem = release_problem(passage, record_id)
    if problem:
        raise ValueError(f"The release cannot be written: it {problem}")
    if not present():
        raise ConfirmationRefused(PROOF_MISSING)  # a person's act needs the workspace's key (#306)
    source = store.snapshot(project_id, "release://" + str(uuid4()), passage, "manual",
                            {"origin": RELEASE_ORIGIN, "role": "user", "record_id": record_id})
    return {"record_id": record_id, "path": path, "release_source_id": source["id"],
            "released": [described(hit) for hit in hits], "reason": reason}
