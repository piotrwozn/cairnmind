"""Read-only text view of a portable memory snapshot.

The view needs no store, engine, service or model: it validates the package with
the same loader as ``memory inspect`` and prints what the package contains.
"""
from __future__ import annotations

from cairnmind.memory import _load_snapshot
from cairnmind.store import STATUSES
from cairnmind.terminal_safe import safe_text

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


SOURCE_TEXT_LIMIT = 1200  # characters printed per source
TOTAL_TEXT_LIMIT = 60000  # characters of source text per run


def _cut(text, limit):
    """Keep at most `limit` characters, ending at a line boundary when one falls in the second half."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    boundary = head.rfind("\n")
    return head[:boundary] if boundary >= limit // 2 else head


def _source_lines(ids, sources, left):
    """Lines that print the text of a record's sources.

    Returns the lines, the text they show (the only text --grep may match), the characters used and whether
    the total bound cut or skipped a text. A source is named, not printed, when it is missing, not text or empty.
    """
    lines, seen, used, limited = [], [], 0, False
    for identifier in dict.fromkeys(ids):
        row = sources.get(identifier)
        name = _line(row["uri"]) if row else _line(identifier)
        text = row.get("content") if row else None
        if row is None:
            lines.append(f"  source {name}: missing from this package, not shown")
        elif not isinstance(text, str) or "\x00" in text:
            lines.append(f"  source {name}: not text, not shown")
        elif not text.strip():
            lines.append(f"  source {name}: empty, not shown")
        elif left - used <= 0:
            limited = True
        else:
            # Bound the text as it will be printed, so that escapes cannot make a line longer than it looks.
            whole, allowed = _block(text).strip("\n"), min(SOURCE_TEXT_LIMIT, left - used)
            kept = _cut(whole, allowed)
            limited = limited or (allowed < SOURCE_TEXT_LIMIT and len(kept) < len(whole))
            lines += [f"  source {name}:", *_indented(kept, "    | ")]
            if len(kept) < len(whole):
                lines.append(f"    ({len(whole) - len(kept)} more characters left out)")
            seen.append(kept)
            used += len(kept)
    return lines, seen, used, limited


def render_snapshot(manifest, payload, *, kind=None, status=None, grep=None, sources_text=False):
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

    candidates = [row for row in records.values() if kind in (None, row["kind"]) and status in (None, row["status"])]
    order = {name: index for index, name in enumerate(RECORD_KINDS)}
    candidates.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    candidates.sort(key=lambda row: order.get(row["kind"], len(order)))
    # Source texts are laid out in the order they will be printed, so that the total bound falls where the
    # reader sees it and --grep can match only text that is on the screen.
    shown_records, texts, left, limited = [], {}, TOTAL_TEXT_LIMIT, False
    for row in candidates:
        lines, seen, used, hit = _source_lines(row["source_ids"], sources, left) if sources_text else ([], [], 0, False)
        if found(row["content"], row["key"], *uris(row["source_ids"]), *seen):
            shown_records.append(row)
            texts[row["id"]] = lines
            left, limited = left - used, limited or hit
    replacing = {}
    for row in records.values():
        if isinstance(row.get("supersedes"), str):
            replacing.setdefault(row["supersedes"], row)  # the first of several is named

    def named(identifier):
        other = records.get(identifier)
        return _line(identifier) + (f" ({_line(other['key'])})" if other else "")
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
                *_indented(row["content"], "  | ")]
        if isinstance(row.get("supersedes"), str):
            out.append(f"  replaces: {named(row['supersedes'])}")
        if row["status"] == "superseded" and row["id"] in replacing:
            out.append(f"  replaced by: {named(replacing[row['id']]['id'])}")
        out += ["  sources: " + ", ".join(_line(uri) for uri in uris(row["source_ids"])), *texts[row["id"]]]
        if row["kind"] != "task":
            continue
        out.append(f"  state: {_line(metadata.get('state', 'todo'))}")
        criteria, links = metadata.get("acceptance_criteria"), metadata.get("criterion_checks")
        if isinstance(criteria, list) and criteria:
            links = links if isinstance(links, dict) else {}
            out.append("  acceptance criteria:")
            out += [f"    - {_line(text)} ({'check: ' + _line(links[text]) if text in links else 'no check'})"
                    for text in criteria]
        if metadata.get("depends_on"):
            out.append("  depends on: " + ", ".join(_line(i) for i in metadata["depends_on"]))
        claims = [label for flag, label in _TASK_CLAIMS if metadata.get(flag) is True]
        if claims:
            out.append("  reported by an agent, not verified by this view: " + ", ".join(claims))
        if runs.get(row["id"]):
            out.append("  historical check runs (carried in this snapshot; not fresh evidence for your checkout):")
            out += [_check_run(run) for run in sorted(runs[row["id"]], key=lambda r: str(r.get("ended_at") or ""),
                                                      reverse=True)]

    if limited:
        out += ["", f"Source texts are limited to {TOTAL_TEXT_LIMIT:,} characters in all; further texts are not shown."]

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


def show_snapshot(path, *, kind=None, status=None, grep=None, sources_text=False, expected_snapshot_id=None):
    """Validate the package exactly as ``inspect_snapshot`` does, then render it."""
    _, _, manifest, payload = _load_snapshot(path)
    if expected_snapshot_id is not None and manifest["snapshot_id"] != expected_snapshot_id:
        raise ValueError("Memory package changed while it was being authorized")
    return render_snapshot(manifest, payload, kind=kind, status=status, grep=grep, sources_text=sources_text)
