"""Trail Notes: a few lines of code, so that the sample memory has something real to point at."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4


def write_note(folder: Path, text: str, tags: list[str] | None = None) -> str:
    """Store one note as one JSON file and return its identifier."""
    identifier = str(uuid4())
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{identifier}.json").write_text(json.dumps({"id": identifier, "text": text, "tags": sorted(tags or [])}))
    return identifier


def read_note(folder: Path, identifier: str) -> dict:
    """Read a note. A note written before tags existed has none."""
    note = json.loads((folder / f"{identifier}.json").read_text())
    note.setdefault("tags", [])
    return note
