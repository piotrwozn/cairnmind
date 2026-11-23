"""Read a JSON file that a pane's process or a fake client writes (#419).

Not collected (no test_ prefix). A writer that opens a file and fills it in two steps leaves it empty for a moment,
so a reader that only waits for the file to exist can read nothing. This waits, bounded, for a complete document and
fails with what the file held.
"""
from __future__ import annotations

import json
import time


def read_json_when_complete(path, seconds=5.0, context=lambda: ""):
    """The document in `path` once it parses; after `seconds`, an AssertionError with the file's text and context()."""
    deadline = time.monotonic() + seconds
    while True:
        text = path.read_text() if path.exists() else None
        if text is not None:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        if time.monotonic() >= deadline:
            raise AssertionError(f"{path} never held a complete JSON document; it is "
                                 + ("missing" if text is None else repr(text)) + context())
        time.sleep(.01)
