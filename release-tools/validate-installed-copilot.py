"""Verify installed account authentication after restart without model inference."""
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path

from devbrain.cockpit_state import CockpitState
from devbrain.config import Settings
from devbrain.copilot_adapter import CopilotAdapter, runtime_status


async def main():
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"):
        os.environ.pop(name, None)
    settings = Settings.load()
    profiles = CockpitState(settings).load()["profiles"]
    profile = next(row for row in profiles if row["id"] == "copilot")
    adapter = CopilotAdapter(profile, None, settings=settings)
    try:
        auth = await adapter.auth_status()
        models = await adapter.list_models()
    finally:
        await adapter.close()
    report = {
        "version": importlib.metadata.version("devbrain"),
        "kernel": os.uname().release,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "runtime": runtime_status(settings),
        "auth": auth,
        "models": models,
        "configured_model": profile["model"],
        "profile_has_no_api_credential": not bool(profile.get("credential_ref")),
        "api_key_provided": False,
        "ambient_github_tokens_removed": True,
        "model_inference": False,
    }
    report["status"] = "passed" if (
        auth["authenticated"] and report["runtime"]["available"]
        and report["profile_has_no_api_credential"]
        and any(row["id"] == profile["model"] and row["enabled"] for row in models)
    ) else "failed"
    target = Path(__file__).resolve().parents[1] / "evidence/installed-copilot-after-restart-20261107.json"
    target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
