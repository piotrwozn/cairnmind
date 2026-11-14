"""What a change to published memory says, for people (#172): `memory diff BASE HEAD`.

Two published trees are compared by ID, with no store, service or model. A new file is added (a
new task is opened, or closed when it arrives done); new revisions of a known file are changed,
superseded or disputed records, or tasks closed or opened again; a new plan definition is a
redefinition; a removed file is withdrawn. A file that keeps only the first of the base's revisions
is reverted (a `git revert` of the change that appended the rest). Published revisions are never
rewritten, so a head whose revisions differ from the base's is an error, and so is a head tree that
would not import. Every entry carries the text it now has and its quoted source.

Text from the files is untrusted. The text form passes it through safe_text. The Markdown form for a
pull request comment puts it in four-space literal blocks (Markdown renders nothing there: no markup,
mentions or links) and short values in code spans; every heading and sentence around them is written
here. The comment is bounded, and what does not fit is folded into <details> or only counted.
"""
from __future__ import annotations

import re

from .memory_published_check import STATIC_FILES, check_files
from .memory_published_format import J, PublishedFormatError, path_of, ref_class, reviewed_by
from .memory_published_parse import parse_file
from .terminal_safe import safe_text

MARKER = "<!-- cairnmind-memory-diff -->"
STATE_MARKER = "<!-- cairnmind-memory-diff-state: {} -->"
MAX_COMMENT_CHARS = 60000
OPEN_CHARS = 20000
BLOCK_LINES = 40
BLOCK_CHARS = 4000
KIND_ORDER = ("decision", "constraint", "fact", "question", "preference", "task", "plan")
ACTIONS = {"confirm": "Confirmed", "dispute": "Disputed", "resolve_conflict": "Chosen in a conflict decision",
           "confirm_plan": "Confirmed with its plan", "workspace_form": "Typed as confirmed in the workspace"}
CATEGORY_ORDER = ("added", "opened", "closed", "changed", "disputed", "superseded", "redefined", "reverted",
                  "withdrawn")
BASE_UNREADABLE = ("The published memory of the base commit could not be read, so every record of the head is "
                   "shown as added.")


def _models(files, strict):
    """({id: model}, {id: path}, errors, skipped). A strict tree is checked whole; a lenient one is read
    file by file, skipping what does not parse (the base of a change is only compared with)."""
    if not files:
        return {}, {}, [], 0
    tree, errors = check_files(files)
    if tree is not None:
        return {**tree["records"], **tree["plans"]}, dict(tree["paths"]), [], 0
    if strict:
        return {}, {}, errors, 0
    models, paths, skipped = {}, {}, 0
    for path, raw in sorted(files.items()):
        if path in STATIC_FILES:
            continue
        try:
            model = parse_file(raw)
        except (PublishedFormatError, ValueError):
            skipped += 1
            continue
        if path != path_of(model) or model["id"] in models:
            skipped += 1
            continue
        models[model["id"]], paths[model["id"]] = model, path
    return models, paths, [], skipped


def _entries_of(model):
    return model["definitions"] if model["kind"] == "plan" else model["revisions"]


def _category(kind, before, after, new):
    if kind == "plan":
        return "added" if new else "redefined"
    if new:
        if kind == "task":
            return "superseded" if after["status"] == "superseded" else "closed" if after["state"] == "done" else "opened"
        return "added"
    if after["status"] == "superseded" and before["status"] != "superseded":
        return "superseded"
    if kind == "task" and after["state"] == "done" and before["state"] != "done":
        return "closed"
    if kind == "task" and before["state"] == "done" and after["state"] != "done":
        return "opened"
    if after["status"] == "disputed" and before["status"] != "disputed":
        return "disputed"
    return "changed"


def _source_label(source):
    ref, kind = source["ref"], ref_class(source["ref"])
    if kind == "file":
        return f"{ref['kind']} {ref['path']}"
    if kind == "note":
        return "a note typed in the workspace"
    if kind == "plan":
        return "the plan"
    return None if kind == "review" else f"{ref.get('kind')} ({ref.get('origin') or 'no origin'})"


def _entry(category, model, path, start, before=None):
    kind, entries = model["kind"], _entries_of(model)
    last, numbers = entries[-1], list(range(start + 1, len(entries) + 1))
    entry = {"category": category, "kind": kind, "id": model["id"], "path": path, "revisions": numbers,
             "reviewed_by": [e for e in reviewed_by(model, entries) if e["revision"] in numbers]}
    if kind == "plan":
        entry.update(title=last["title"], text=last["goal"], status=None, state=None, quote=None,
                     sources=[], previous_text=before["goal"] if before and before["goal"] != last["goal"] else None,
                     steps=len(last["steps"]))
        return entry
    quote = last["quote"]
    if quote is not None:
        ref = model["sources"][quote["source"]]["ref"]
        quote = {"text": quote["text"], "lines": quote["lines"], "path": ref.get("path"), "commit": ref.get("commit"),
                 "source": _source_label(model["sources"][quote["source"]])}
    labels = [_source_label(model["sources"][s]) for s in last["sources"]]
    entry.update(text=last["text"], status=last["status"], state=last.get("state"), quote=quote,
                 sources=[label for label in labels if label],
                 previous_text=before["text"] if before and before["text"] != last["text"] else None,
                 previous_status=before["status"] if before else None,
                 previous_state=before.get("state") if before else None)
    return entry


def compare(base_files, head_files) -> dict:
    """The change from one published tree ({path: bytes}) to another, as data."""
    head, head_paths, errors, _ = _models(head_files, True)
    if errors:
        return {"status": "refused", "errors": errors, "entries": [], "counts": [], "skipped": 0}
    base, base_paths, _, skipped = _models(base_files, False)
    entries = []
    for identifier in sorted(set(base) | set(head)):
        old, new = base.get(identifier), head.get(identifier)
        if new is None:
            entries.append(_entry("withdrawn", old, base_paths[identifier], len(_entries_of(old)) - 1))
            continue
        path = head_paths[identifier]
        if old is None:
            entries.append(_entry(_category(new["kind"], None, _entries_of(new)[-1], True), new, path, 0))
            continue
        before, after = _entries_of(old), _entries_of(new)
        if old["kind"] == new["kind"] and len(after) < len(before) and all(
                J(a) == J(b) for a, b in zip(before, after)):
            # The head keeps the base's first revisions and drops the rest: a revert of the change that appended
            # them. The head tree imports (a store that has them stays ahead), so it is reported, not refused.
            entry = _entry("reverted", new, path, len(after) - 1, before[-1])
            entry.update(revisions=list(range(len(after) + 1, len(before) + 1)), reviewed_by=[])
            entries.append(entry)
            continue
        if old["kind"] != new["kind"] or len(after) < len(before) or any(
                J(a) != J(b) for a, b in zip(before, after)):
            errors.append((path, "an earlier published revision was rewritten or removed; published revisions "
                                 "are only ever appended"))
            continue
        if len(after) > len(before):
            category = _category(new["kind"], before[-1], after[-1], False)
            entries.append(_entry(category, new, path, len(before), before[-1]))
    if errors:
        return {"status": "refused", "errors": errors, "entries": [], "counts": [], "skipped": skipped}
    order = {name: index for index, name in enumerate(CATEGORY_ORDER)}
    kinds = {name: index for index, name in enumerate(KIND_ORDER)}
    entries.sort(key=lambda e: (kinds[e["kind"]], order[e["category"]], e["id"]))
    counts = {}
    for entry in entries:
        counts[(entry["kind"], entry["category"])] = counts.get((entry["kind"], entry["category"]), 0) + 1
    return {"status": "changed" if entries else "unchanged", "errors": [], "entries": entries,
            "counts": [{"kind": k, "category": c, "count": n} for (k, c), n in counts.items()], "skipped": skipped}


def summary(report) -> str:
    if report["status"] == "refused":
        return f"The published memory is refused: {len(report['errors'])} problem(s)."
    if not report["counts"]:
        return "No published memory changed."
    parts = [f"{c['count']} {c['kind']}{'' if c['count'] == 1 else 's'} {c['category']}" for c in report["counts"]]
    return ", ".join(parts) + "."


def _who(entry):
    return [(item["name"], item.get("branch"), ACTIONS.get(item["action"], "Reviewed"))
            for item in entry["reviewed_by"]]


def _heading(entry):
    title = f"{entry['kind'].capitalize()} {entry['id'][:8]} {entry['category']}"
    numbers = entry["revisions"]
    word = "definition" if entry["kind"] == "plan" else "revision"
    span = f"{word} {numbers[0]}" if len(numbers) == 1 else f"{word}s {numbers[0]}-{numbers[-1]}"
    details = [{"withdrawn": f"last {span}", "reverted": f"{span} removed"}.get(entry["category"], span)]
    if entry.get("status"):
        details.append(entry["status"] if entry.get("previous_status") in (None, entry["status"])
                       else f"{entry['previous_status']} -> {entry['status']}")
    if entry["kind"] == "task" and entry.get("state"):
        details.append(f"state {entry['state']}" if entry.get("previous_state") in (None, entry["state"])
                       else f"state {entry['previous_state']} -> {entry['state']}")
    return title, details


def render_text(report, base, head) -> str:
    """The comparison for a terminal; every value from a file through safe_text."""
    lines = [f"Published memory from {base[:12]} to {head[:12]}: {summary(report)}"]
    for path, reason in report["errors"]:
        lines.append(safe_text(f"  {path or '(tree)'}: {reason}"))
    if report.get("skipped"):
        lines.append(f"  {report['skipped']} file(s) of the base could not be read and were left out.")
    if report.get("base_unreadable"):
        lines.append(f"  {BASE_UNREADABLE}")

    def indented(label, text):
        lines.append(f"  {label}:")
        lines.extend(("    " + part) if part else "" for part in safe_text(text).split("\n"))

    for entry in report["entries"]:
        title, details = _heading(entry)
        lines += ["", f"{title} ({', '.join(details)}): {safe_text(entry['path'])}"]
        for name, branch, action in _who(entry):
            where = f" on {branch}" if branch else ""
            lines.append(safe_text(f"  {action} by {name}{where} (claimed by the file; not verified here)"))
        if entry["kind"] == "plan":
            lines.append(safe_text(f"  Title: {entry['title']}; {entry['steps']} step(s)"))
        indented("Goal" if entry["kind"] == "plan" else "Text", entry["text"])
        if entry.get("previous_text") is not None:
            indented("Before", entry["previous_text"])
        if entry.get("quote"):
            quote = entry["quote"]
            lines.append(safe_text(f"  Quoted from {quote['source']}, lines {quote['lines'][0]}-{quote['lines'][1]}"
                                   + (f" at commit {quote['commit'][:12]}" if quote.get("commit") else "") + ":"))
            lines.extend(("    " + part) if part else "" for part in safe_text(quote["text"]).split("\n"))
        elif entry.get("sources"):
            lines.append(safe_text("  Sources: " + "; ".join(entry["sources"])))
    return "\n".join(lines) + "\n"


def code_span(value, limit=120) -> str:
    """A short untrusted value as one inline code span: Markdown renders nothing inside it."""
    text = " ".join(safe_text(value).split())
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    if not text:
        return "(empty)"
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text[0] == "`" or text[-1] == "`" else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def literal_block(text) -> list[str]:
    """Untrusted text as a four-space literal block, bounded; the note about what was cut is ours."""
    parts = safe_text(text).split("\n")
    cut = 0
    if len(parts) > BLOCK_LINES:
        parts, cut = parts[:BLOCK_LINES], len(parts) - BLOCK_LINES
    kept, size = [], 0
    for part in parts:
        if size + len(part) > BLOCK_CHARS:
            part = part[:max(0, BLOCK_CHARS - size)] + "…"
            kept.append(part)
            cut += len(parts) - len(kept)
            break
        kept.append(part)
        size += len(part) + 1
    lines = ["", *(("    " + part) if part.strip() else "" for part in kept), ""]
    if cut:
        lines += [f"({cut} more line{'' if cut == 1 else 's'} not shown; run `cairnmind memory diff` to read all)", ""]
    return lines


def _markdown_entry(entry) -> str:
    title, details = _heading(entry)
    said = ", ".join(details)
    lines = [f"#### {title}", "", f"{said[0].upper()}{said[1:]}. File {code_span(entry['path'])}."]
    for name, branch, action in _who(entry):
        where = f" on {code_span(branch)}" if branch else ""
        lines.append(f"{action} by {code_span(name)}{where} "
                     "(claimed by the file; not verified here).")
    if entry["kind"] == "plan":
        lines += ["", f"Title {code_span(entry['title'])}, {entry['steps']} step(s). Goal:"]
    else:
        lines += ["", "Text:"]
    lines += literal_block(entry["text"])
    if entry.get("previous_text") is not None:
        lines += ["Before:", *literal_block(entry["previous_text"])]
    if entry.get("quote"):
        quote = entry["quote"]
        commit = f" at commit {code_span(quote['commit'][:12])}" if quote.get("commit") else ""
        lines += [f"Quoted from {code_span(quote['source'])}, lines {quote['lines'][0]}-{quote['lines'][1]}{commit}:",
                  *literal_block(quote["text"])]
    elif entry.get("sources"):
        lines += ["Sources: " + ", ".join(code_span(label) for label in entry["sources"]) + ".", ""]
    return "\n".join(lines).rstrip("\n") + "\n\n"


def render_markdown(report, base, head, max_chars=MAX_COMMENT_CHARS) -> str:
    """The pull request comment: at most max_chars characters, the first entries open, more folded."""
    max_chars = max(2000, min(int(max_chars), MAX_COMMENT_CHARS))
    state = report["status"]
    out = [MARKER, STATE_MARKER.format(state), "### Team memory in this pull request", "",
           f"Published memory from {code_span(base[:12])} to {code_span(head[:12])}: {summary(report)}", ""]
    if state == "refused":
        out += ["The published memory directory would not import. Nothing in it can be read until it is fixed:"]
        listing = "\n".join(f"{path or '(tree)'}: {reason}" for path, reason in report["errors"][:200])
        out += literal_block(listing)
    if report.get("skipped"):
        out += [f"{report['skipped']} file(s) of the base commit could not be read and were left out.", ""]
    if report.get("base_unreadable"):
        out += [BASE_UNREADABLE, ""]
    if state == "changed":
        out += ["Names, branches and texts below are what the files say. Nothing here is verified.", ""]
    text = "\n".join(out) + "\n"
    rendered = [_markdown_entry(entry) for entry in report["entries"]]
    closing = "</details>\n"
    reserve = 400
    index = 0
    while index < len(rendered) and len(text) + len(rendered[index]) <= min(OPEN_CHARS, max_chars - reserve):
        text += rendered[index]
        index += 1
    if index < len(rendered):
        folded = ""
        start = index
        while index < len(rendered) and (len(text) + len(folded) + len(rendered[index]) + len(closing) + 200
                                         <= max_chars - reserve):
            folded += rendered[index]
            index += 1
        if folded:
            text += f"<details><summary>{index - start} more entr{'y' if index - start == 1 else 'ies'}</summary>\n\n"
            text += folded + closing + "\n"
    if index < len(rendered):
        left = len(rendered) - index
        text += (f"{left} more entr{'y is' if left == 1 else 'ies are'} not shown here; run "
                 f"`cairnmind memory diff {base[:12]} {head[:12]}` to read all.\n")
    return text[:max_chars]
