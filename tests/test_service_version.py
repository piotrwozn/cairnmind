"""A knowledge service of another version gets nothing from a client (#319)."""
from __future__ import annotations

import re

import httpx
import pytest

from cairnmind import __version__
from cairnmind.cli_cmds import service_version
from cairnmind.cli_cmds.service_version import NO_HEALTH, OTHER_VERSION, VERSION_UNKNOWN, instance_id, refusal
from cairnmind.config import Settings
from cairnmind.pal.process_control import format_command
from cairnmind.terminal.terminal_i18n import CATALOG


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRNMIND_PORT", "1")
    return Settings.load(tmp_path / "data", create=True)


REAL = httpx.Client


def answer(monkeypatch, handler):
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: REAL(transport=httpx.MockTransport(handler), **kwargs))


def health(settings, **changes):
    body = {"service": "cairnmind", "version": __version__, "instance": instance_id(settings), "backend": "arcadedb"}
    return {key: value for key, value in {**body, **changes}.items() if value is not None}


def command(settings):
    return format_command(["cairnmind", "--data-dir", str(settings.data_dir), "stop"])


def test_a_service_of_this_version_or_none_at_all_is_no_reason(settings, monkeypatch):
    answer(monkeypatch, lambda request: httpx.Response(200, json=health(settings)))
    assert refusal(settings) is None

    def nothing(request):
        raise httpx.ConnectError("refused")

    answer(monkeypatch, nothing)
    assert refusal(settings) is None


def test_a_service_of_another_version_is_named_with_the_command_that_stops_it(settings, monkeypatch):
    answer(monkeypatch, lambda request: httpx.Response(200, json=health(settings, version="0.11.0")))
    assert refusal(settings) == OTHER_VERSION.format(service="0.11.0", client=__version__, command=command(settings))


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"service": "cairnmind", "version": "0.1.0", "instance": "someone-else"}),
    httpx.Response(200, json={"service": "other", "version": "0.1.0", "instance": "x"}),
    httpx.Response(200, text="<html>hello</html>"),
    httpx.Response(200, json=["cairnmind"]),
])
def test_another_data_directory_or_another_program_is_left_to_the_token_check(settings, monkeypatch, response):
    answer(monkeypatch, lambda request: response)
    assert refusal(settings) is None


@pytest.mark.parametrize("change", [
    {"version": None}, {"version": 7}, {"version": "0.12.0\x1b[31m"}, {"version": "1" * 41},
])
def test_a_service_that_does_not_say_its_version_gets_nothing(settings, monkeypatch, change):
    answer(monkeypatch, lambda request: httpx.Response(200, json=health(settings, **change)))
    reason = refusal(settings)
    assert reason == VERSION_UNKNOWN.format(url=settings.url, command=command(settings))
    assert "\x1b" not in reason and "1" * 41 not in reason


def test_a_program_that_does_not_answer_is_not_called_the_service_and_stop_is_not_advised(settings, monkeypatch):
    def hang(request):
        raise httpx.ReadTimeout("slow")

    answer(monkeypatch, hang)
    reason = refusal(settings)
    assert reason == NO_HEALTH.format(url=settings.url, data_dir=settings.data_dir)
    assert " stop" not in reason and "knowledge service at" not in reason
    # doctor cannot name the program on the port, so it is not where the person is sent (#319 recheck)
    assert "doctor" not in reason and "config.json" in reason


def test_the_sentence_is_shown_in_polish_by_the_workspace(settings, monkeypatch):
    from cairnmind.cli import ServiceOfOtherVersion
    from cairnmind.terminal.terminal_i18n import tr
    from cairnmind.terminal.terminal_i18n_messages import broker_error_text
    answer(monkeypatch, lambda request: httpx.Response(200, json=health(settings, version="0.11.0")))
    error = ServiceOfOtherVersion(refusal(settings))
    english = broker_error_text(error, lambda text: tr(text, "en"))
    polish = broker_error_text(error, lambda text: tr(text, "pl"))
    assert english == str(error) and polish != english
    assert polish.startswith("Usługa wiedzy tego katalogu") and "0.11.0" in polish and command(settings) in polish


def test_every_version_sentence_has_its_polish_in_the_terminal_catalog():
    placeholders = lambda text: sorted(re.findall(r"\{(\w+)\}", text))  # noqa: E731
    assert service_version.MESSAGES
    for english, polish in service_version.MESSAGES.items():
        assert CATALOG[english] == polish != english
        assert placeholders(english) == placeholders(polish)
