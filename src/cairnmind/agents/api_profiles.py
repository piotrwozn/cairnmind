"""Validated owner-local profiles for native clients and API coding agents.

Profiles contain credential references, never credential values.  Loading or
normalizing a profile does not initialize a service or make a network request.
"""
from __future__ import annotations

from copy import deepcopy
import ipaddress
import math
import re
from urllib.parse import urlsplit, urlunsplit


PROFILE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,199}$")
PROVIDERS = {
    "openai": {"protocol": "openai_responses", "base_url": "https://api.openai.com/v1"},
    "anthropic": {"protocol": "anthropic_messages", "base_url": "https://api.anthropic.com/v1"},
    "openai_compatible": {"protocol": "openai_chat_completions", "base_url": ""},
    "ollama": {"protocol": "ollama_chat", "base_url": "http://127.0.0.1:11434"},
}
API_LIMITS = {"max_model_calls": 20, "max_output_tokens": 4096, "command_timeout_seconds": 120}
COPILOT_LIMITS = {"max_tool_calls": 20, "max_turn_seconds": 900, "command_timeout_seconds": 120}
_RANGES = {"max_model_calls": (1, 1000), "max_output_tokens": (1, 262144),
           "command_timeout_seconds": (1, 3600), "max_tool_calls": (1, 10000),
           "max_turn_seconds": (1, 86400), "context_window_tokens": (4096, 262144)}


def normalize_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ValueError("Give a valid API address.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise ValueError("Give a valid API address.") from None
    if (parsed.scheme not in {"https", "http"} or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or "\\" in value or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("The API address must not hold a key, a password or query parameters.")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.casefold() == "localhost"
        if not loopback:
            raise ValueError("Plain HTTP is allowed only for localhost; use HTTPS.")
    if any(p in {".", ".."} for p in parsed.path.split("/")):
        raise ValueError("Invalid API path.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _limits(value, defaults):
    if value is None:
        return dict(defaults)
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError("Invalid profile limits.")
    result = dict(defaults)
    for name, number in value.items():
        minimum, maximum = _RANGES[name]
        if type(number) is not int or not minimum <= number <= maximum:
            raise ValueError("A profile limit is outside the allowed range.")
        result[name] = number
    return result


def _credential_ref(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Give a credential reference, not an API key.")
    kind, separator, name = value.partition(":")
    valid = ((kind == "env" and ENV_NAME.fullmatch(name))
             or (kind == "profile" and PROFILE_ID.fullmatch(name)))
    if not separator or not valid:
        raise ValueError("A credential reference is env:NAME or profile:IDENTIFIER.")
    return value


def normalize_profile(row: dict) -> dict:
    """Return a canonical safe profile; refuse unknown fields and secret values."""
    if (not isinstance(row, dict) or not {"id", "client", "label"} <= set(row)
            or not isinstance(row["id"], str) or not PROFILE_ID.fullmatch(row["id"])
            or not isinstance(row["label"], str) or not 1 <= len(row["label"].strip()) <= 80
            or any(ord(c) < 32 or ord(c) == 127 for c in row["label"])):
        raise ValueError("Invalid client profile.")
    client = row["client"]
    if not isinstance(client, str) or client not in {"codex", "claude", "api", "copilot"}:
        raise ValueError("Choose a supported client or API provider.")
    common = {"id", "client", "label", "model"}
    allowed = common if client in {"codex", "claude"} else (
        common | {"credential_ref", "limits"} if client == "copilot" else
        common | {"provider", "protocol", "base_url", "credential_ref", "limits", "pricing"})
    if set(row) - allowed:
        raise ValueError("The profile has unsupported fields; save keys separately.")
    result = deepcopy(row)
    result["label"] = row["label"].strip()
    model = row.get("model")
    if model is not None and (not isinstance(model, str) or not MODEL_ID.fullmatch(model)):
        raise ValueError("Invalid model identifier.")
    if client in {"codex", "claude"}:
        return result
    result["credential_ref"] = _credential_ref(row.get("credential_ref"))
    if client == "copilot":
        result["limits"] = _limits(row.get("limits"), COPILOT_LIMITS)
        return result
    provider = row.get("provider")
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError("Choose an API provider.")
    if not model:
        raise ValueError("An API profile needs a model identifier.")
    settings = PROVIDERS[provider]
    if row.get("protocol", settings["protocol"]) != settings["protocol"]:
        raise ValueError("The protocol does not match the chosen provider.")
    result["protocol"] = settings["protocol"]
    result["base_url"] = normalize_base_url(row.get("base_url", settings["base_url"]))
    defaults = {**API_LIMITS, **({"context_window_tokens": 32768} if provider == "ollama" else {})}
    result["limits"] = _limits(row.get("limits"), defaults)
    if result["credential_ref"] is None and provider in {"openai", "anthropic"}:
        result["credential_ref"] = "profile:" + row["id"]
    if "pricing" in row:
        pricing = row["pricing"]
        if (not isinstance(pricing, dict) or set(pricing) != {"input_per_million", "output_per_million"}
                or any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1_000_000
                       for x in pricing.values())):
            raise ValueError("Give both non-negative rates per million tokens.")
        result["pricing"] = dict(pricing)
    return result
