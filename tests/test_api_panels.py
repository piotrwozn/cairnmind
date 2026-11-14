"""Terminal-only API profile configuration is explicit and write-only for keys."""
from copy import deepcopy

import pytest
from textual.widgets import Input, Static, Switch

from cairnmind.terminal_panels import CredentialModal, SidebarApp, profile_fields, profile_from_form
from cairnmind.terminal_ui import FormModal
from test_terminal_panels import setup, settle


class ProfileBroker:
    def __init__(self, inner, profiles):
        self.inner, self.profiles = inner, profiles
        self.calls = inner.calls

    def ensure(self):
        return self.inner.ensure()

    def request(self, method, path, *, params=None, json=None, timeout=20):
        if path == "/api/state":
            value = self.inner.request(method, path, params=params, json=json)
            value["profiles"] = deepcopy(self.profiles)
            return value
        if path == "/api/profiles" and method == "PUT":
            self.calls.append((method, path, params or {}, deepcopy(json)))
            self.profiles = deepcopy(json["profiles"])
            return {"profiles": self.profiles}
        if path.endswith("/models"):
            self.calls.append((method, path, params or {}, deepcopy(json)))
            return {"models": [{"id": "coding-model"}]}
        return self.inner.request(method, path, params=params, json=json)


@pytest.mark.parametrize("provider", ["ollama", "openai", "anthropic", "openai_compatible"])
def test_provider_forms_are_editable_and_do_not_embed_keys(provider):
    fields = profile_fields(None, client="api", provider=provider)
    value = {f.name: f.value for f in fields}
    value.update(label="Local model", model="coding", credential_ref="env:MY_API_KEY")
    row = profile_from_form(value, identifier="my-api", client="api", provider=provider)
    assert row["provider"] == provider
    expected = {"max_model_calls": 20, "max_output_tokens": 4096, "command_timeout_seconds": 120}
    if provider == "ollama":
        expected["context_window_tokens"] = 32768
    assert row["limits"] == expected
    assert row["credential_ref"] == "env:MY_API_KEY"
    assert "key" not in row
    assert not any(f.name in {"key", "api_key"} for f in fields)


def test_copilot_form_has_runtime_limits_without_fake_api_url():
    fields = profile_fields(None, client="copilot")
    value = {f.name: f.value for f in fields}
    value["label"] = "Copilot"
    row = profile_from_form(value, identifier="copilot-p", client="copilot")
    assert row["limits"]["max_tool_calls"] == 20
    assert row["limits"]["max_turn_seconds"] == 900
    assert "base_url" not in row


@pytest.mark.asyncio
async def test_credentials_are_masked_ephemeral_by_default_and_cleared_on_cancel(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.push_screen(CredentialModal("Provider [bold]"))
        await pilot.pause()
        widget = app.screen.query_one("#credential-key", Input)
        assert widget.password is True
        assert app.screen.query_one("#credential-persist", Switch).value is False
        widget.value = "super-secret-fixture"
        lines = "\n".join(s.text for s in app.screen._compositor.render_strips())
        assert "super-secret-fixture" not in lines
        assert "nie jest szyfrowany" in lines
        await pilot.click("#credential-cancel")
        await pilot.pause()
        assert widget.value == ""
        assert not any("credential" in path for _, path, _, _ in broker.calls)


@pytest.mark.asyncio
async def test_new_ollama_profile_saved_without_model_request_or_client_start(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    responses = iter(["+", "ollama", {"label": "Local", "model": "coder", "base_url": "http://127.0.0.1:11434",
                       "credential_ref": "", "max_model_calls": "20", "max_output_tokens": "4096", "context_window_tokens": "32768", "command_timeout_seconds": "120"}])
    async def choose(screen):
        value = next(responses)
        if isinstance(screen, FormModal) and isinstance(value, dict):
            assert screen.on_submit is not None
            await screen.on_submit(value)
        return value
    monkeypatch.setattr(app, "push_screen_wait", choose)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.edit_profiles()
        await settle(app, pilot)
        writes = [body for method, path, _, body in broker.calls if method == "PUT" and path == "/api/profiles"]
        assert writes[0]["profiles"][-1]["provider"] == "ollama"
        assert not any(path.endswith("/probe") or path == "/api/client-runs" for _, path, _, _ in broker.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier,advanced,title", [
    ("+", False, "Add connection"), ("api-p", False, "Edit connection"),
    ("api-p", True, "Advanced limits and credentials")])
async def test_the_connection_form_title_says_what_it_does(tmp_path, monkeypatch, identifier, advanced, title):
    settings, inner, workspace = setup(tmp_path, monkeypatch)
    broker = ProfileBroker(inner, [{"id": "api-p", "label": "API", "client": "api", "provider": "openai"}])
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    titles = []

    async def choose(screen):
        if isinstance(screen, FormModal):
            titles.append(screen.title_text)
            return None
        return "ollama"  # the connection type of a new one
    monkeypatch.setattr(app, "push_screen_wait", choose)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.edit_profiles(identifier, advanced=advanced)
        await settle(app, pilot)
    assert titles == [title]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,title", [("language", "Language"), ("view", "Layout"),
                                          ("reduced_motion", "Reduce motion")])
async def test_a_setting_picker_is_titled_with_the_setting(tmp_path, monkeypatch, choice, title):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    headings = []

    async def choose(screen):
        headings.append(screen.heading)
        return choice if len(headings) == 1 else None
    monkeypatch.setattr(app, "push_screen_wait", choose)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.action_settings()
        await settle(app, pilot)
    assert headings == ["Settings", title]


@pytest.mark.asyncio
async def test_probe_cancel_never_calls_provider_and_confirm_calls_once(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    profile = {"id": "api-p", "label": "API", "client": "api", "provider": "openai"}
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    responses = iter(["probe", False])
    async def choose(screen):
        return next(responses)
    monkeypatch.setattr(app, "push_screen_wait", choose)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        await app.profile_connection(profile)
        assert not any(path.endswith("/probe") for _, path, _, _ in broker.calls)
        responses = iter(["probe", True, None])
        await app.profile_connection(profile)
        assert len([p for m, p, _, _ in broker.calls if m == "POST" and p.endswith("/probe")]) == 1


@pytest.mark.asyncio
async def test_model_selection_updates_profile_without_starting_generation(tmp_path, monkeypatch):
    settings, inner, workspace = setup(tmp_path, monkeypatch)
    profile = {"id": "api-p", "label": "API", "client": "api", "provider": "openai", "model": "old"}
    broker = ProfileBroker(inner, [profile])
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    responses = iter(["models", "coding-model"])
    async def choose(screen):
        return next(responses)
    monkeypatch.setattr(app, "push_screen_wait", choose)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        await app.profile_connection(profile)
        assert broker.profiles[0]["model"] == "coding-model"
        assert not any(path.endswith("/probe") or path == "/api/client-runs" for _, path, _, _ in broker.calls)
        assert "nie uruchomiono" in str(app.query_one("#panel-notice", Static).render())
