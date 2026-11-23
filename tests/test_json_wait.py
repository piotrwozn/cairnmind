"""The wait for a JSON file that another process writes (#419)."""
import json
import threading
import time

import pytest

from json_wait import read_json_when_complete


def test_a_file_created_empty_and_filled_later_is_waited_for(tmp_path):
    path = tmp_path / "capture.json"
    path.write_text("")  # what a writer that opens, then fills, leaves for a moment
    with pytest.raises(json.JSONDecodeError):
        json.loads(path.read_text())  # the failure of the two CI runs

    def fill():
        time.sleep(.3)
        path.write_text('{"argv": []}')
    threading.Thread(target=fill).start()
    assert read_json_when_complete(path, 5) == {"argv": []}


@pytest.mark.parametrize("held", [None, "", '{"argv": ['])
def test_a_file_that_never_completes_fails_with_what_it_held(tmp_path, held):
    path = tmp_path / "capture.json"
    if held is not None:
        path.write_text(held)
    with pytest.raises(AssertionError) as raised:
        read_json_when_complete(path, .2, lambda: " PANES")
    assert "never held a complete JSON document" in str(raised.value) and str(raised.value).endswith(" PANES")
    assert ("missing" if held is None else repr(held)) in str(raised.value)
