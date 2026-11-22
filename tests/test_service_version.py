"""A knowledge service of another version gets nothing from a client (#319)."""
from __future__ import annotations

import re
import shlex

import httpx
import pytest

from cairnmind import __version__, service_version
from cairnmind.config import Settings
from cairnmind.service_version import OTHER_VERSION, VERSION_UNKNOWN, instance_id, refusal
from cairnmind.terminal_i18n import CATALOG


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
    return f"cairnmind --data-dir {shlex.quote(str(settings.data_dir))} stop"


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
    {"version": None}, {"version": 7}, {"version": "0.12.0\x1b[31m"}, {"version": "1" * 41}, "timeout",
])
def test_a_service_that_does_not_say_its_version_gets_nothing(settings, monkeypatch, change):
    def handler(request):
        if change == "timeout":
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json=health(settings, **change))

    answer(monkeypatch, handler)
    reason = refusal(settings)
    assert reason == VERSION_UNKNOWN.format(url=settings.url, command=command(settings))
    assert "\x1b" not in reason and "1" * 41 not in reason


def test_every_version_sentence_has_its_polish_in_the_terminal_catalog():
    placeholders = lambda text: sorted(re.findall(r"\{(\w+)\}", text))  # noqa: E731
    assert service_version.MESSAGES
    for english, polish in service_version.MESSAGES.items():
        assert CATALOG[english] == polish != english
        assert placeholders(english) == placeholders(polish)
