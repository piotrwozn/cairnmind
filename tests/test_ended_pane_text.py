"""The pane of an ended session says so in a sentence of ours, not tmux's "Pane is dead" (#169).

Real tmux 3.4+ and a fake client (the fixture of test_client_runtime); runs the same on Linux and macOS.
"""
from __future__ import annotations

import subprocess
import time

from cairnmind.terminal import terminal_i18n
from cairnmind.agents.client_runtime import ENDED
from test_client_runtime import native as native  # noqa: F401 (fixture)


def start_session(runtime, project, slot="slot-native"):
    row = runtime.start(project, "codex", slot_id=slot, profile_id="codex")
    session = "cairnmind_" + row["id"]
    deadline = time.monotonic() + 8
    # Input sent before the fake client sets its terminal mode is flushed with it.
    while "NATIVE_READY" not in runtime._tmux("capture-pane", "-p", "-t", session).stdout and time.monotonic() < deadline:
        time.sleep(.05)
    return session


def diagnosis(runtime, session, sends):
    """What a timed-out end looks like, so a failing run names its cause: panes, the client's process, each send-keys."""
    panes = runtime._tmux("list-panes", "-a", "-F", "#{session_name} dead=#{pane_dead} status=#{pane_dead_status} pid=#{pane_pid}", check=False)
    pid = next((int(p.split("pid=")[1]) for p in panes.stdout.splitlines() if p.startswith(session + " ") and p.split("pid=")[1]), 0)
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip() or "gone" if pid else "no pid"
    return f"DUMP panes={panes.stdout.split(chr(10))} client_state={state} send_keys={sends}"


def end_session(runtime, session):
    deadline, resend, sends = time.monotonic() + 10, 0, []
    while True:  # tmux draws the sentence a moment after the pane is marked dead
        text = " ".join(runtime._tmux("capture-pane", "-p", "-t", session).stdout.split())
        if "Session ended" in text or "Sesja zakończona" in text or "Pane is dead" in text:
            return text
        if time.monotonic() > deadline:
            return f"{text} {diagnosis(runtime, session, sends)}"
        if time.monotonic() > resend:  # a loaded machine sometimes loses the first line
            sent = runtime._tmux("send-keys", "-t", session, "quit", "Enter", check=False)
            sends.append((sent.returncode, sent.stderr))
            # What the workspace does when it looks at the run: a pane that died without a status gets one (test below).
            runtime._pane(next(r for r in runtime._read() if r["tmux_session"] == session))
            resend = time.monotonic() + 1.5
        time.sleep(.05)


def ended_pane(runtime, project):
    return end_session(runtime, start_session(runtime, project))


def test_an_ended_session_shows_our_sentence_and_how_it_ended(native):
    runtime, project, _ = native
    text = ended_pane(runtime, project)
    assert "Session ended (exit status 0). Resume it from Sessions." in text
    assert "Pane is dead" not in text


def test_each_session_keeps_the_language_it_started_in(native, monkeypatch):
    # tmux reads the format when the pane dies, so a server-wide format would show the latest start's language.
    runtime, project, _ = native
    language = ["en"]
    monkeypatch.setattr(terminal_i18n, "language_for", lambda settings: language[0])
    english = start_session(runtime, project, "slot-one")
    language[0] = "pl"
    polish = start_session(runtime, project, "slot-two")
    assert "Session ended (exit status 0). Resume it from Sessions." in end_session(runtime, english)
    assert "Sesja zakończona (kod wyjścia 0)" in end_session(runtime, polish)


def test_a_sign_in_run_is_not_told_to_resume_it(native):
    runtime, _, _ = native
    session = "cairnmind_" + runtime.start_auth("codex")["id"]  # the fixture's login prints a code and exits
    text = end_session(runtime, session)
    assert "Session ended (exit status 0)." in text and "Resume" not in text


def test_a_pane_dead_without_a_status_is_given_one_by_a_job_of_the_servers_own(native, monkeypatch):
    # tmux 3.4 sometimes misses the SIGCHLD of an exited pane and reaps it only when another child of its server ends.
    runtime, project, _ = native
    row = runtime.start(project, "codex", slot_id="slot-native", profile_id="codex")
    real, seen = runtime._tmux, []

    def tmux(*args, check=True):
        seen.append(args[0])
        result = real(*args, check=check)
        if seen.count("display-message") == 1 and args[0] == "display-message":  # the zombie, as tmux then reports it
            result.stdout = f"{row['tmux_session']}\t{row['id']}\t{row['generation']}\t%0\t4242\t1\t\n"
        return result

    monkeypatch.setattr(runtime, "_tmux", tmux)
    pane = runtime._pane(row)
    assert seen == ["display-message", "run-shell", "display-message"]
    assert pane["dead"] is False  # the second answer is the live client's


def test_the_sentence_has_polish_and_keeps_tmux_formats_intact():
    polish = terminal_i18n.tr(ENDED, "pl")
    assert polish != ENDED and "Wznów" in polish
    for text in (ENDED, polish):  # the same tmux format variables in both
        assert "#{pane_dead_signal}" in text and "#{pane_dead_status}" in text
