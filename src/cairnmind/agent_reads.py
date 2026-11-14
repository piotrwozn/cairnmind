"""What file content the model has actually seen in its current context.

write_file with an expected_sha256 replaces a whole existing file. The hash alone
proves only that the file did not change since some read; it does not prove the
model saw all of it. A read that was cut for the context (or a line slice) still
carries the whole-file hash, and a small model then writes back the part it saw
as if it were the file (#157). This ledger records, per file version, which lines
reached the model whole, so a whole-file replacement can require a complete read.
"""
from __future__ import annotations

MAX_REPORTED_GAPS = 8


def _merge(spans):
    merged = []
    for first, last in sorted(spans):
        if merged and first <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], last)
        else:
            merged.append([first, last])
    return merged


def normalized_path(value):
    from .agent_tools import ToolError, _parts

    try:
        return "/".join(_parts(value))
    except ToolError:
        return None


def _whole_rows(content, complete):
    """How many rows of `content` arrived whole: a row needs its line end, unless nothing was cut."""
    rows = content.splitlines(keepends=True)
    if rows and not complete and rows[-1].splitlines()[0] == rows[-1]:
        rows.pop()
    return len(rows)


def projected_read(projection, original=None):
    """A read_file result cut for the context must not claim to be complete.

    end_line then names the last line the cut content still holds whole, or is left out when none
    is (#157). `original` is the executor's result before the cut. When that result held one line,
    or part of one, and still did not fit, that line is longer than a tool result can carry: the
    file can never be read whole here, and the result says so instead of asking for smaller slices."""
    projection["truncated"] = True
    content, first = projection.get("content"), projection.get("start_line")
    full = original.get("content") if isinstance(original, dict) else None
    last = original.get("end_line") if isinstance(original, dict) else None
    projection.pop("end_line", None)
    whole = 0
    if isinstance(content, str) and isinstance(full, str) and type(first) is int and type(last) is int:
        whole = min(_whole_rows(content, content == full), max(last - first + 1, 0))
        if whole:
            projection["end_line"] = first - 1 + whole
    if not whole and isinstance(full, str) and len(full.splitlines()) <= 1 and type(first) is int:
        projection["line_too_long"] = True
        projection["next_step"] = (
            f"Line {first} of this file is longer than one tool result can carry, so this file cannot be "
            "read whole in this conversation. Change it with apply_patch.")
        return projection
    projection["next_step"] = (
        "This read was cut to fit the context; you did not receive the whole file. Read it again in "
        "smaller slices with start_line and max_lines, or change it with apply_patch.")
    return projection


class ReadLedger:
    def __init__(self, budget=None):
        self._files = {}  # path -> {"sha256", "total", "spans"}; one version per path
        # Characters the whole context may hold before it must be compacted (which clears this ledger), or
        # None when the provider keeps its own context. A file of at least this many bytes is taken as too
        # large to read whole in one context: there each character costs at least one and a line end three,
        # so this holds for any text that is not mostly multi-byte characters.
        self._budget = budget

    def clear(self):
        """A renewed model context no longer holds what was read before."""
        self._files.clear()

    def _entry(self, path, digest, total):
        entry = self._files.get(path)
        if entry is None or entry["sha256"] != digest:
            entry = self._files[path] = {"sha256": digest, "total": total, "spans": []}
        return entry

    def _whole(self, path, digest):
        entry = self._files.get(path)
        if entry is None or entry["sha256"] != digest:
            return False
        return entry["total"] == 0 or _merge(entry["spans"]) == [[1, entry["total"]]]

    def observe(self, name, delivered, *, masked=False):
        """Record a host file tool result exactly as it was delivered to the model. `masked` is True when
        redaction changed the result, so the model did not see the real text."""
        if not isinstance(delivered, dict) or delivered.get("ok") is not True:
            return
        path, digest = delivered.get("path"), delivered.get("sha256")
        if not isinstance(path, str):
            return
        if name == "delete_file":
            self._files.pop(path, None)
            return
        if not isinstance(digest, str):
            return
        if name == "write_file":
            # The model wrote every byte of this version itself.
            self._files[path] = {"sha256": digest, "total": 0, "spans": [], "authored": True}
        elif name == "apply_patch":
            known = self._whole(path, delivered.get("previous_sha256"))
            self._files.pop(path, None)
            if known:
                self._files[path] = {"sha256": digest, "total": 0, "spans": [], "authored": True}
        elif name == "read_file":
            size, total = delivered.get("bytes"), delivered.get("total_lines")
            too_large = delivered.get("line_too_long") is True or (
                self._budget is not None and type(size) is int and size >= self._budget)
            if too_large and type(total) is int:
                entry = self._entry(path, digest, total)
                entry["too_large"] = not entry.get("authored")
            if not delivered.get("host_context_truncated") and not masked and delivered.get("masked") is not True:
                self._read(path, digest, delivered)

    def _read(self, path, digest, delivered):
        content, first, total = delivered.get("content"), delivered.get("start_line"), delivered.get("total_lines")
        last = delivered.get("end_line")
        if (not isinstance(content, str) or any(type(value) is not int for value in (first, total, last))
                or first < 1 or not 0 <= last <= total or len(content.splitlines()) < last - first + 1):
            return
        entry = self._entry(path, digest, total)
        if entry.get("authored"):
            return
        if last >= first:
            entry["spans"].append((first, last))

    def refusal(self, path_argument, expected, tool="write_file"):
        """None when replacing (write_file) or removing (delete_file) the whole file is allowed, otherwise a
        tool error the model can act on. A delete is held to the same rule: otherwise delete_file and an
        exclusive create would replace a file with the part that was seen, without any approval (#157)."""
        path = normalized_path(path_argument)
        if path is None or expected is None:
            return None  # An invalid path, or an exclusive create, is the executor's to decide.
        entry = self._files.get(path)
        if entry is not None and entry["sha256"] == expected and (entry.get("authored") or self._whole(path, expected)):
            return None
        if entry is not None and entry["sha256"] == expected and entry.get("too_large"):
            # Reading it in slices cannot help, so the refusal does not send the model round that loop.
            effect = ("delete_file removes" if tool == "delete_file" else "write_file replaces") + (
                " the whole file, which needs all of it read in this conversation first. This file cannot be "
                "read whole in this conversation: it is larger than the context can hold, or one of its lines "
                "is longer than a tool result can carry. Do not read it again for this. For a partial change "
                "use apply_patch; ") + ("the file cannot be deleted with delete_file here."
                                       if tool == "delete_file" else "a whole replacement is not possible here.")
            return {"ok": False, "error_code": "full_read_required", "reason": "file_cannot_be_read_whole",
                    "path": path, "total_lines": entry["total"], "message": effect}
        if tool == "delete_file":
            effect = ("delete_file removes the whole file, but the complete current content of this file has not "
                      "reached you in this conversation (it was not read, read only in part, cut to fit the "
                      "context, or masked). Deleting it would lose what you have not seen. For a local change use apply_patch. "
                      "To delete the file, first read all of it with read_file")
        else:
            effect = ("write_file replaces the whole file, but the complete current content of this file has not "
                      "reached you in this conversation (it was not read, read only in part, cut to fit the "
                      "context, or masked). Writing it would drop what you have not seen. For a local change use "
                      "apply_patch. To rewrite the file, first read all of it with read_file")
        error = {"ok": False, "error_code": "full_read_required", "path": path, "message": effect + (
            ", in slices with start_line and max_lines if it is long, so that every result arrives whole.")}
        if entry is not None and entry["sha256"] == expected and entry["total"]:
            gaps, line = [], 1
            for first, last in _merge(entry["spans"]):
                if first > line:
                    gaps.append([line, first - 1])
                line = last + 1
            if line <= entry["total"]:
                gaps.append([line, entry["total"]])
            error.update(total_lines=entry["total"], unread_lines=gaps[:MAX_REPORTED_GAPS])
        return error
