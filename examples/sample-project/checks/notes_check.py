"""The check of the sample project: run by the profile `unit` in .cairnmind/checks.toml."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trail_notes import read_note, write_note  # noqa: E402


class Notes(unittest.TestCase):
    def test_a_note_keeps_its_text_and_tags(self):
        with tempfile.TemporaryDirectory() as folder:
            identifier = write_note(Path(folder), "Ford the river above the bridge.", ["route", "water"])
            self.assertEqual(read_note(Path(folder), identifier)["tags"], ["route", "water"])

    def test_a_note_written_before_tags_existed_still_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "old.json").write_text(json.dumps({"id": "old", "text": "Camp at the saddle."}))
            self.assertEqual(read_note(Path(folder), "old")["tags"], [])


if __name__ == "__main__":
    unittest.main()
