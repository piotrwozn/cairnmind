"""The snapshot text view: pure rendering, with no store, engine or service."""
from __future__ import annotations

import pytest

from cairnmind import memory_view
from cairnmind.memory_view import render_snapshot


def _record(identifier, kind, content, *, status="confirmed", sources=("s1",), updated="2026-11-01T10:00:00+00:00",
            supersedes=None, **metadata):
    return {"id": identifier, "project_id": "p", "kind": kind, "status": status, "key": f"key-{identifier}",
            "content": content, "version": 2, "updated_at": updated, "source_ids": list(sources),
            "supersedes": supersedes, "metadata": metadata}


@pytest.fixture
def snapshot():
    payload = {
        "sources": [{"id": "s1", "uri": "chat://session/1", "kind": "conversation",
                     "content": "Meeting, 1 November: keep login errors in one module.\nOwner: Mira."},
                    {"id": "s2", "uri": "file://src/login.py", "kind": "code",
                     "content": "def login():\n    return None"}],
        "records": [
            _record("d1", "decision", "Login errors are handled in auth/errors.py.", sources=("s1", "s2")),
            _record("f1", "fact", "The release identifier is zephyr_214.", status="proposed"),
            _record("t1", "task", "Add the retry limit\nwith a test.", state="in_progress", test_passed=True),
            _record("t2", "task", "Document the retry limit.", depends_on=["t1"]),
        ],
        "plans": [{"id": "plan1", "title": "Harden login", "goal": "No endless retries.", "version": 3,
                   "constraints": ["Keep the public API."], "step_ids": ["t1", "t2"], "source_ids": ["s1"]}],
        "check_runs": [{"id": "run1", "task_id": "t1", "profile_id": "unit", "passed": True, "outcome": "completed",
                        "returncode": 0, "ended_at": "2026-11-01T11:00:00+00:00", "origin": "local_runner"},
                       {"id": "run0", "task_id": "t1", "profile_id": "unit", "passed": False, "outcome": "completed",
                        "returncode": 1, "ended_at": "2026-11-01T09:00:00+00:00", "origin": "local_runner"}],
    }
    manifest = {"project": {"id": "p", "name": "shop"}, "snapshot_id": "a" * 64, "created_at": "2026-11-02T08:00:00+00:00",
                "origin_root": "/home/ana/src/shop", "active_plan_id": "plan1",
                "counts": {"records": 4, "plans": 1, "sources": 2, "check_runs": 2},
                "completeness": {"pending_analysis": 1}}
    return manifest, payload


def test_header_names_the_snapshot_and_says_the_view_is_read_only(snapshot):
    text = render_snapshot(*snapshot)
    assert text.splitlines()[0] == "Portable memory snapshot (read-only view; nothing is imported or written)"
    assert "project:   shop (p)" in text
    assert f"snapshot:  {'a' * 64}" in text
    assert "created:   2026-11-02T08:00:00+00:00" in text
    assert "origin:    /home/ana/src/shop" in text
    assert "contains:  4 records, 1 plan, 2 sources, 2 historical check runs" in text
    assert "note:      1 source still waited for analysis" in text
    assert "filters:" not in text


def test_records_show_kind_status_version_time_and_source_uris(snapshot):
    text = render_snapshot(*snapshot)
    assert "Records (4 shown of 4)" in text
    assert "[decision] confirmed, version 2, updated 2026-11-01T10:00:00+00:00" in text
    assert "  | Login errors are handled in auth/errors.py." in text
    assert "  id: d1  key: key-d1" in text
    assert "  sources: chat://session/1, file://src/login.py" in text
    assert text.index("[decision]") < text.index("[task]") < text.index("[fact]")


def test_kind_status_and_grep_filters_combine(snapshot):
    manifest, payload = snapshot
    text = render_snapshot(manifest, payload, kind="decision", grep="LOGIN")
    assert "Records (1 shown of 4)" in text and "auth/errors.py" in text
    assert "zephyr_214" not in text and "Plans" not in text
    assert "filters:   kind=decision grep=LOGIN" in text
    assert "zephyr_214" in render_snapshot(manifest, payload, status="proposed")
    assert "auth/errors.py" not in render_snapshot(manifest, payload, status="proposed")
    # A source URI is searchable, because "which records came from this file" is a real question.
    assert "Records (1 shown of 4)" in render_snapshot(manifest, payload, grep="src/login.py")
    assert "Nothing in this snapshot matches the filters." in render_snapshot(manifest, payload, grep="absent-word")


def test_plan_lists_ordered_steps_with_their_states(snapshot):
    manifest, payload = snapshot
    text = render_snapshot(manifest, payload, kind="plan")
    assert "Records" not in text
    assert "[plan] Harden login, version 3, active in the origin worktree" in text
    assert "  | No endless retries." in text and "  constraint: Keep the public API." in text
    assert "  1. [in_progress] Add the retry limit (t1)" in text
    assert "  2. [todo] Document the retry limit. (t2)" in text
    assert "  sources: chat://session/1" in text
    assert "Plans (1 shown of 1)" in render_snapshot(manifest, payload, grep="document the retry")
    # A plan is listed only for text the view prints: not for a later line of a step.
    assert "Plans (0 shown of 1)" in render_snapshot(manifest, payload, kind="plan", grep="with a test")
    assert "Plans (1 shown of 1)" in render_snapshot(manifest, payload, kind="plan", grep="chat://session")
    assert "Plans (0 shown of 1)" in render_snapshot(manifest, payload, grep="zephyr")


def test_check_runs_are_labelled_historical_and_never_fresh(snapshot):
    text = render_snapshot(*snapshot, kind="task")
    assert "historical check runs (carried in this snapshot; not fresh evidence for your checkout):" in text
    newest, older = text.index("run run1"), text.index("run run0")
    assert newest < older
    assert "unit: passed, ended 2026-11-01T11:00:00+00:00, recorded by local_runner, run run1" in text
    assert "unit: not passed (completed, exit 1)" in text
    assert "fresh" not in text.replace("not fresh evidence", "")


def test_a_passed_flag_contradicted_by_outcome_or_exit_code_is_not_shown_as_passed(snapshot):
    manifest, payload = snapshot
    payload["check_runs"][0].update(outcome="timeout", returncode=1)
    payload["check_runs"][1].update(passed=True, returncode=False)
    text = render_snapshot(manifest, payload, kind="task")
    assert "unit: not passed (timeout, exit 1)" in text and "unit: not passed (completed, exit False)" in text
    assert "unit: passed" not in text


def test_a_forged_exit_code_cannot_leave_its_line(snapshot):
    manifest, payload = snapshot
    payload["check_runs"][1]["returncode"] = "0)\x1b[2K\r      unit: passed, FRESH\n\n[decision] confirmed"
    text = render_snapshot(manifest, payload)
    assert "\x1b" not in text and "\r" not in text and "\n[decision] confirmed, FRESH" not in text
    assert sum(line.startswith("[decision]") for line in text.splitlines()) == 1


def test_task_claims_are_marked_as_reported_and_dependencies_are_listed(snapshot):
    text = render_snapshot(*snapshot, kind="task")
    assert "  state: in_progress" in text
    assert "  reported by an agent, not verified by this view: test passed" in text
    assert "  depends on: t1" in text


def test_terminal_control_and_bidi_characters_never_reach_the_output(snapshot):
    manifest, payload = snapshot
    payload["records"][0]["content"] = "safe\x1b[2J\x1b]0;title\x07 \u202egnp.exe\r\nnext\u2028more"
    payload["sources"][0]["uri"] = "chat://x\x1b[31m\nforged: line"
    manifest["project"]["name"] = "shop\x1b[0m\nproject:   forged"
    text = render_snapshot(manifest, payload)
    assert not any(ch in text for ch in "\x1b\x07\r\u202e\u2028")
    assert "  | safe\\x1b[2J\\x1b]0;title\\x07 \\u202egnp.exe" in text and "  | next\\u2028more" in text
    assert "\nforged: line" not in text and "\nproject:   forged" not in text


@pytest.mark.parametrize("options, message", [
    ({"kind": "note"}, "Unknown kind"), ({"status": "fresh"}, "Unknown status"),
    ({"kind": "plan", "status": "confirmed"}, "Plans have no status")])
def test_unknown_filters_are_rejected(snapshot, options, message):
    with pytest.raises(ValueError, match=message):
        render_snapshot(*snapshot, **options)


def test_empty_snapshot_says_so():
    manifest = {"project": {"id": "p", "name": "empty"}, "snapshot_id": "b" * 64, "origin_root": "/x",
                "counts": {"records": 0, "plans": 0, "sources": 0, "check_runs": 0}}
    payload = {"sources": [], "records": [], "plans": [], "check_runs": []}
    text = render_snapshot(manifest, payload)
    assert "created:   unknown" in text and text.endswith("This snapshot is empty.")
    payload["sources"] = [{"id": "s1", "uri": "file://a.py", "kind": "code"}]
    manifest["counts"]["sources"] = 1
    assert render_snapshot(manifest, payload).endswith("This snapshot has no records or plans yet (1 source).")


def _block(text, identifier):
    """The lines printed for one record, from its [kind] line to the blank line after it."""
    for block in text.split("\n\n"):
        if f"  id: {identifier}" in block:
            return block
    raise AssertionError(f"{identifier} is not in the output")


def test_sources_prints_the_text_of_a_source_under_the_record_it_supports(snapshot):
    manifest, payload = snapshot
    block = _block(render_snapshot(manifest, payload, kind="decision", sources_text=True), "d1")
    lines = block.splitlines()
    heading = lines.index("  source chat://session/1:")
    assert lines[heading - 1] == "  sources: chat://session/1, file://src/login.py"
    assert lines[heading + 1:heading + 3] == ["    | Meeting, 1 November: keep login errors in one module.", "    | Owner: Mira."]
    assert "  source file://src/login.py:" in lines and "    |     return None" in lines
    # Without the option nothing of a source is printed but its address.
    plain = render_snapshot(manifest, payload, kind="decision")
    assert "Meeting, 1 November" not in plain and "return None" not in plain and "  source chat" not in plain
    # A source that supports several records is printed under each.
    text = render_snapshot(manifest, payload, sources_text=True)
    assert text.count("    | Owner: Mira.") == 4 and text.count("  source chat://session/1:") == 4


def test_a_long_source_is_cut_at_a_line_boundary_and_says_how_much_was_left_out(snapshot):
    manifest, payload = snapshot
    assert (memory_view.SOURCE_TEXT_LIMIT, memory_view.TOTAL_TEXT_LIMIT) == (1200, 60000)
    payload["sources"][0]["content"] = "abcdefghi\n" * 300
    block = _block(render_snapshot(manifest, payload, kind="decision", sources_text=True), "d1")
    printed = [line for line in block.splitlines() if line.startswith("    | abcdefghi")]
    # 120 whole lines of 9 characters fit in 1,200 characters; the other 1,800 are left out.
    assert len(printed) == 120 and all(line == "    | abcdefghi" for line in printed)
    assert "    (1800 more characters left out)" in block


def test_the_total_of_source_text_is_bounded_and_the_note_comes_once(snapshot, monkeypatch):
    manifest, payload = snapshot
    monkeypatch.setattr(memory_view, "TOTAL_TEXT_LIMIT", 1500)
    payload["sources"][0]["content"], payload["sources"][1]["content"] = "a" * 1000, "b" * 1000
    text = render_snapshot(manifest, payload, sources_text=True)
    assert "a" * 1000 in text and "b" * 500 in text and "b" * 501 not in text
    assert "    (500 more characters left out)" in text
    assert text.count("Source texts are limited to 1,500 characters in all; further texts are not shown.") == 1
    # The later records that rest on the same source get no text at all once the bound is reached.
    assert text.count("a" * 1000) == 1
    # The note does not depend on a record being shown: the bound may have hidden what would have matched.
    assert "Source texts are limited" in render_snapshot(manifest, payload, kind="decision", grep="zzz", sources_text=True)


def test_source_text_cannot_carry_terminal_escapes_or_forged_lines(snapshot):
    manifest, payload = snapshot
    payload["sources"][0]["content"] = "safe\x1b[2J\x1b]0;title\x07 ‮gnp.exe\r\n\x1b[31mforged: line more\rend"
    text = render_snapshot(manifest, payload, kind="decision", sources_text=True)
    assert not any(ch in text for ch in "\x1b\x07\r‮ ")
    assert "    | safe\\x1b[2J\\x1b]0;title\\x07 \\u202egnp.exe" in text
    assert "    | \\x1b[31mforged: line\\u2028more\\x0dend" in text
    assert "\nforged: line" not in text


@pytest.mark.parametrize("content, reason", [("binary\x00data", "not text"), (None, "not text"), (7, "not text"),
                                             ("  \n\t", "empty")])
def test_a_source_that_is_not_text_or_empty_is_named_and_not_printed(snapshot, content, reason):
    manifest, payload = snapshot
    payload["sources"][0]["content"] = content
    text = render_snapshot(manifest, payload, kind="decision", sources_text=True)
    assert f"  source chat://session/1: {reason}, not shown" in text
    assert "binary" not in text and "    | 7" not in text
    assert "    | def login():" in text


def test_a_source_missing_from_the_package_is_named_by_its_identifier(snapshot):
    manifest, payload = snapshot
    payload["records"][0]["source_ids"] = ["s1", "ghost"]
    text = render_snapshot(manifest, payload, kind="decision", sources_text=True)
    assert "  source ghost: missing from this package, not shown" in text
    assert "  sources: chat://session/1" in text and "ghost" not in text.split("  source ghost")[0]


def test_a_task_shows_its_acceptance_criteria_and_the_check_each_is_linked_to(snapshot):
    manifest, payload = snapshot
    payload["records"][2]["metadata"].update(
        acceptance_criteria=["The unit check passes.", "An old file\nstill loads."],
        criterion_checks={"The unit check passes.": "unit"})
    text = render_snapshot(manifest, payload, kind="task")
    block = _block(text, "t1")
    assert "  acceptance criteria:\n    - The unit check passes. (check: unit)\n" \
           "    - An old file still loads. (no check)\n" in block
    # A task without criteria says nothing about them.
    assert "acceptance criteria" not in _block(text, "t2")
    payload["records"][2]["metadata"]["acceptance_criteria"] = []
    assert "acceptance criteria" not in render_snapshot(manifest, payload, kind="task")


def test_a_record_names_the_record_it_replaced_and_the_one_that_replaced_it(snapshot):
    manifest, payload = snapshot
    payload["records"][0]["status"] = "superseded"
    payload["records"].append(_record("d2", "decision", "Login errors live in auth/login.py.", supersedes="d1"))
    text = render_snapshot(manifest, payload, kind="decision")
    assert "  replaced by: d2 (key-d2)\n" in _block(text, "d1") and "replaces" not in _block(text, "d1")
    assert "  replaces: d1 (key-d1)\n" in _block(text, "d2") and "replaced by" not in _block(text, "d2")
    # Only a record that is marked superseded says that something replaced it.
    payload["records"][0]["status"] = "confirmed"
    assert "replaced by" not in render_snapshot(manifest, payload, kind="decision")
    # The replacing record is not in the package: nothing to name.
    payload["records"][0]["status"] = "superseded"
    del payload["records"][-1]
    assert "replaced by" not in render_snapshot(manifest, payload, kind="decision")


def test_grep_reads_source_text_only_when_it_is_shown(snapshot):
    manifest, payload = snapshot
    payload["sources"][1]["content"] = "def login():\n    return quokka_77\n" + "x\n" * 700 + "hidden_tail_88\n"
    assert "Nothing in this snapshot matches" in render_snapshot(manifest, payload, grep="quokka_77")
    text = render_snapshot(manifest, payload, grep="quokka_77", sources_text=True)
    assert "Records (1 shown of 4)" in text and "    |     return quokka_77" in text
    # Text beyond the bound is not on the screen, so it does not match; neither does text of a non-text source.
    assert "Nothing in this snapshot matches" in render_snapshot(manifest, payload, grep="hidden_tail_88",
                                                                 sources_text=True)
    payload["sources"][0]["content"] = "binary\x00quokka_99"
    assert "Nothing in this snapshot matches" in render_snapshot(manifest, payload, grep="quokka_99",
                                                                 sources_text=True)


def test_output_without_the_new_option_does_not_depend_on_source_text(snapshot):
    manifest, payload = snapshot
    with_text = render_snapshot(manifest, payload, sources_text=False)
    for row in payload["sources"]:
        del row["content"]
    assert render_snapshot(manifest, payload) == with_text
    assert "replaces" not in with_text and "replaced by" not in with_text and "acceptance criteria" not in with_text


def test_the_total_bound_note_is_printed_even_when_the_record_it_hid_is_not_shown(snapshot):
    manifest, payload = snapshot
    # 51 records, each with its own 1,200-character source that matches: the 50 first fill the 60,000 bound.
    payload["sources"] = [{"id": f"x{n}", "uri": f"note://{n}", "kind": "note", "content": "shared " + "x" * 1193}
                          for n in range(51)]
    payload["records"] = [_record(f"r{n}", "fact", f"Fact {n}.", sources=(f"x{n}",),
                                  updated=f"2026-11-01T10:{59 - n % 60:02d}:00+00:00") for n in range(51)]
    payload["plans"] = []
    text = render_snapshot(manifest, payload, grep="shared", sources_text=True)
    # The last record matches only through a text the bound cut away, so it is not shown, and the note says why.
    assert "Records (50 shown of 51)" in text and "Fact 50." not in text
    assert text.count("Source texts are limited to 60,000 characters in all; further texts are not shown.") == 1


def test_a_cut_never_ends_inside_an_escape(snapshot, monkeypatch):
    manifest, payload = snapshot
    payload["sources"][0]["content"] = "a" * 1199 + "‮" + "b" * 10
    text = render_snapshot(manifest, payload, kind="decision", sources_text=True)
    assert "    | " + "a" * 1199 + "\n" in text and "a\\\n" not in text
    # 6 characters of the escape and the 10 after it are left out, and the count says so.
    assert "    (16 more characters left out)" in text
    monkeypatch.setattr(memory_view, "TOTAL_TEXT_LIMIT", 500)
    payload["sources"][0]["content"] = "a" * 499 + "\x1b[31m" + "b" * 10
    text = render_snapshot(manifest, payload, kind="decision", sources_text=True)
    assert "    | " + "a" * 499 + "\n" in text and "    (18 more characters left out)" in text


def test_a_source_cited_by_many_records_is_escaped_once(snapshot, monkeypatch):
    manifest, payload = snapshot
    big = "a line of source text\n" * 1000
    payload["sources"][0]["content"] = big
    payload["records"] += [_record(f"m{n}", "fact", f"More {n}.", sources=("s1",)) for n in range(30)]
    escaped = []
    original = memory_view._block
    monkeypatch.setattr(memory_view, "_block", lambda value: escaped.append(len(str(value))) or original(value))
    render_snapshot(manifest, payload, sources_text=True)
    render_snapshot(manifest, payload, grep="no such text", sources_text=True)
    assert [size for size in escaped if size >= len(big)] == [len(big)] * 2


def test_replacement_by_conflict_resolution_is_shown_from_the_notes_it_leaves(snapshot):
    manifest, payload = snapshot
    # conflicts.py: the loser gets metadata.superseded_by and the winner metadata.superseded_ids; no link.
    payload["records"] = [
        _record("d0", "decision", "Zero.", status="superseded"),
        _record("d1", "decision", "One.", status="superseded", superseded_by="d2"),
        _record("d3", "decision", "Three.", status="superseded", superseded_by=5),
        _record("d2", "decision", "Two.", supersedes="d0", superseded_ids=["d1", "d0", "d1", 5, "", None, "d3"]),
        _record("d4", "decision", "Four.", supersedes="d1")]
    text = render_snapshot(manifest, payload, kind="decision")
    assert "  replaces: d0 (key-d0), d1 (key-d1), d3 (key-d3)\n" in _block(text, "d2")
    # The inbox's note comes before the back-link: d4 also points at d1, but d2 replaced it.
    assert "  replaced by: d2 (key-d2)\n" in _block(text, "d1")
    # A note that is not an identifier is ignored and the back-link is used, if there is one.
    assert "replaced by" not in _block(text, "d3")
    payload["records"][2]["metadata"]["superseded_by"] = ""
    payload["records"][4]["supersedes"] = "d3"
    assert "  replaced by: d4 (key-d4)\n" in _block(render_snapshot(manifest, payload, kind="decision"), "d3")


@pytest.mark.parametrize("empty", ["", 5, None])
def test_an_empty_or_odd_supersedes_prints_no_replacement_line(snapshot, empty):
    manifest, payload = snapshot
    payload["records"][0]["status"] = "superseded"
    payload["records"][1]["supersedes"] = empty
    text = render_snapshot(manifest, payload)
    assert "replaces" not in text and "replaced by" not in text


def test_grep_also_finds_the_criteria_and_check_names_printed_for_a_task(snapshot):
    manifest, payload = snapshot
    payload["records"][2]["metadata"].update(acceptance_criteria=["Retries stop after five attempts."],
                                             criterion_checks={"Retries stop after five attempts.": "unit_retry"})
    for word in ("STOP AFTER FIVE", "unit_retry"):
        text = render_snapshot(manifest, payload, grep=word)
        assert "Records (1 shown of 4)" in text and "  id: t1" in text
    assert "Nothing in this snapshot matches" in render_snapshot(manifest, payload, kind="decision", grep="unit_retry")


def test_a_text_of_which_no_character_fits_is_not_printed_as_an_empty_block(snapshot, monkeypatch):
    manifest, payload = snapshot
    monkeypatch.setattr(memory_view, "TOTAL_TEXT_LIMIT", 3)
    payload["sources"][0]["content"] = "\x1b[31mred"
    text = render_snapshot(manifest, payload, sources_text=True)
    assert "  source chat://session/1:" not in text and "  source file://src/login.py:" not in text
    assert "    | \n" not in text and "more characters left out" not in text and "    | def login" not in text
    assert text.count("Source texts are limited to 3 characters in all; further texts are not shown.") == 1


def test_grep_matches_a_criterion_as_it_is_printed(snapshot):
    manifest, payload = snapshot
    payload["records"][2]["metadata"].update(acceptance_criteria=["Retries stop\nafter  five tries."])
    text = render_snapshot(manifest, payload, grep="stop after five")
    assert "    - Retries stop after five tries. (no check)" in text and "  id: t1" in text
