"""Read-only text view of a portable memory snapshot.

The view needs no store, engine, service or model: it validates the package with
the same loader as ``memory inspect`` and prints what the package contains.
"""
from __future__ import annotations

from devbrain.memory import _load_snapshot
from devbrain.store import STATUSES
from devbrain.terminal_safe import safe_text

RECORD_KINDS = ("decision", "constraint", "task", "fact", "question", "preference", "checkpoint")
SHOW_KINDS = (*RECORD_KINDS, "plan")
_TASK_CLAIMS = (("test_passed", "test passed"), ("review_passed", "review passed"),
                ("acceptance_passed", "acceptance passed"))


def _block(value):
    # A snapshot is foreign data. Control and bidirectional-override characters could
    # rewrite the terminal or disguise text, so they are printed as visible escapes.
    text = safe_text(str(value).replace("\r\n", "\n"))
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _line(value):
    return " ".join(_block(value).split())


def _indented(value, prefix):
    return [prefix + line for line in _block(value).strip("\n").split("\n")]


def _first_line(value):
    return _line(str(value).strip().split("\n", 1)[0])


def _plural(count, noun):
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _check_run(run):
    code = run.get("returncode")
    # The same condition as the verifier: a bare "passed" flag is not enough.
    if run.get("passed") is True and run.get("outcome") == "completed" and type(code) is int and code == 0:
        result = "passed"
    else:
        result = (f"not passed ({_line(run.get('outcome', 'unknown'))}"
                  + (f", exit {_line(code)})" if code is not None else ")"))
    return (f"      {_line(run.get('profile_id', '?'))}: {result}, ended {_line(run.get('ended_at', 'unknown'))}, "
            f"recorded by {_line(run.get('origin', 'unknown'))}, run {_line(run['id'])}")


def render_snapshot(manifest, payload, *, kind=None, status=None, grep=None):
    """Return the text view of an already validated snapshot."""
    if kind is not None and kind not in SHOW_KINDS:
        raise ValueError(f"Unknown kind {kind!r}; choose one of: {', '.join(SHOW_KINDS)}")
    if status is not None and status not in STATUSES:
        raise ValueError(f"Unknown status {status!r}; choose one of: {', '.join(sorted(STATUSES))}")
    if kind == "plan" and status is not None:
        raise ValueError("Plans have no status; use --status with records only")
    needle = grep.casefold() if grep else None
    sources = {row["id"]: row for row in payload["sources"]}
    records = {row["id"]: row for row in payload["records"]}
    runs = {}
    for run in payload["check_runs"]:
        runs.setdefault(run["task_id"], []).append(run)

    def uris(source_ids):
        return [sources[i]["uri"] for i in source_ids if i in sources]

    def found(*texts):
        return needle is None or any(needle in str(text).casefold() for text in texts)

    shown_records = [
        row for row in records.values()
        if kind in (None, row["kind"]) and status in (None, row["status"])
        and found(row["content"], row["key"], *uris(row["source_ids"]))]
    order = {name: index for index, name in enumerate(RECORD_KINDS)}
    shown_records.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    shown_records.sort(key=lambda row: order.get(row["kind"], len(order)))
    shown_plans = [] if kind not in (None, "plan") or status is not None else [
        row for row in sorted(payload["plans"], key=lambda row: (row["title"], row["id"]))
        if found(row["title"], row["goal"], *row["constraints"],
                 *(_first_line(records[step]["content"]) for step in row["step_ids"]), *uris(row["source_ids"]))]

    project, counts = manifest["project"], manifest["counts"]
    out = ["Portable memory snapshot (read-only view; nothing is imported or written)",
           f"project:   {_line(project['name'])} ({_line(project['id'])})",
           f"snapshot:  {_line(manifest['snapshot_id'])}",
           f"created:   {_line(manifest.get('created_at') or 'unknown')}",
           f"origin:    {_line(manifest['origin_root'])}",
           "contains:  " + ", ".join((_plural(counts["records"], "record"), _plural(counts["plans"], "plan"),
                                       _plural(counts["sources"], "source"),
                                       _plural(counts["check_runs"], "historical check run")))]
    completeness = manifest.get("completeness")
    waiting = completeness.get("pending_analysis") if isinstance(completeness, dict) else None
    if type(waiting) is int and waiting > 0:
        out.append(f"note:      {_plural(waiting, 'source')} still waited for analysis when the snapshot was taken")
    filters = [f"{name}={_line(value)}" for name, value in (("kind", kind), ("status", status), ("grep", grep)) if value]
    if filters:
        out.append("filters:   " + " ".join(filters))

    if kind != "plan":
        out += ["", f"Records ({len(shown_records)} shown of {len(records)})"]
    for row in shown_records:
        metadata = row["metadata"]
        out += ["", f"[{row['kind']}] {row['status']}, version {row['version']}, "
                    f"updated {_line(row.get('updated_at') or 'unknown')}",
                f"  id: {_line(row['id'])}" + ("" if row["key"] == row["id"] else f"  key: {_line(row['key'])}"),
                *_indented(row["content"], "  | "),
                "  sources: " + ", ".join(_line(uri) for uri in uris(row["source_ids"]))]
        if row["kind"] != "task":
            continue
        out.append(f"  state: {_line(metadata.get('state', 'todo'))}")
        if metadata.get("depends_on"):
            out.append("  depends on: " + ", ".join(_line(i) for i in metadata["depends_on"]))
        claims = [label for flag, label in _TASK_CLAIMS if metadata.get(flag) is True]
        if claims:
            out.append("  reported by an agent, not verified by this view: " + ", ".join(claims))
        if runs.get(row["id"]):
            out.append("  historical check runs (carried in this snapshot; not fresh evidence for your checkout):")
            out += [_check_run(run) for run in sorted(runs[row["id"]], key=lambda r: str(r.get("ended_at") or ""),
                                                      reverse=True)]

    if kind in (None, "plan") and status is None:
        out += ["", f"Plans ({len(shown_plans)} shown of {len(payload['plans'])})"]
    for row in shown_plans:
        active = ", active in the origin worktree" if row["id"] == manifest.get("active_plan_id") else ""
        out += ["", f"[plan] {_line(row['title'])}, version {row['version']}{active}", f"  id: {_line(row['id'])}"]
        if row["goal"].strip():
            out += ["  goal:", *_indented(row["goal"], "  | ")]
        out += [f"  constraint: {_line(item)}" for item in row["constraints"]]
        if uris(row["source_ids"]):
            out.append("  sources: " + ", ".join(_line(uri) for uri in uris(row["source_ids"])))
        for number, step in enumerate(row["step_ids"], 1):
            task = records[step]
            out.append(f"  {number}. [{_line(task['metadata'].get('state', 'todo'))}] "
                       f"{_first_line(task['content'])} ({_line(step)})")
    if not shown_records and not shown_plans:
        if filters:
            out += ["", "Nothing in this snapshot matches the filters."]
        elif payload["sources"]:
            out += ["", f"This snapshot has no records or plans yet ({_plural(len(payload['sources']), 'source')})."]
        else:
            out += ["", "This snapshot is empty."]
    return "\n".join(out)


def show_snapshot(path, *, kind=None, status=None, grep=None, expected_snapshot_id=None):
    """Validate the package exactly as ``inspect_snapshot`` does, then render it."""
    _, _, manifest, payload = _load_snapshot(path)
    if expected_snapshot_id is not None and manifest["snapshot_id"] != expected_snapshot_id:
        raise ValueError("Memory package changed while it was being authorized")
    return render_snapshot(manifest, payload, kind=kind, status=status, grep=grep)
