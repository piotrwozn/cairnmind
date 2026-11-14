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


def projected_read(projection):
    """A read_file result cut for the context must not claim to be complete."""
    projection["truncated"] = True
    projection["next_step"] = (
        "This read was cut to fit the context; you did not receive the whole file. Read it again in "
        "smaller slices with start_line and max_lines, or change it with apply_patch.")
    return projection


class ReadLedger:
    def __init__(self):
        self._files = {}  # path -> {"sha256", "total", "spans"}; one version per path

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

    def observe(self, name, delivered):
        """Record a host file tool result exactly as it was delivered to the model."""
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
        elif name == "read_file" and not delivered.get("host_context_truncated"):
            self._read(path, digest, delivered)

    def _read(self, path, digest, delivered):
        content, first, total = delivered.get("content"), delivered.get("start_line"), delivered.get("total_lines")
        last = delivered.get("end_line")
        if (not isinstance(content, str) or any(type(value) is not int for value in (first, total, last))
                or first < 1 or not 0 <= last <= total or len(content.splitlines()) < last - first + 1):
            return
        if "[REDACTED]" in content:
            return  # A registered secret was masked: the model did not see the real text.
        entry = self._entry(path, digest, total)
        if entry.get("authored"):
            return
        if last >= first:
            entry["spans"].append((first, last))

    def refusal(self, path_argument, expected):
        """None when a whole-file replacement is allowed, otherwise a tool error the model can act on."""
        path = normalized_path(path_argument)
        if path is None or expected is None:
            return None  # An invalid path, or an exclusive create, is the executor's to decide.
        entry = self._files.get(path)
        if entry is not None and entry["sha256"] == expected and (entry.get("authored") or self._whole(path, expected)):
            return None
        error = {"ok": False, "error_code": "full_read_required", "path": path, "message": (
            "write_file replaces the whole file, but the complete current content of this file has not "
            "reached you in this conversation (it was not read, read only in part, or cut to fit the "
            "context). Writing it would drop what you have not seen. For a local change use apply_patch. "
            "To rewrite the file, first read all of it with read_file, in slices with start_line and "
            "max_lines if it is long, so that every result arrives whole.")}
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
