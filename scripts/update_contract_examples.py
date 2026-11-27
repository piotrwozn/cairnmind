"""Regenerate frozen CLI --json and MCP tool contract examples under tests/fixtures/contracts/ (#249)."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from cairnmind.config import Settings  # noqa: E402
from cairnmind.storage.engine import ensure_engine, install_engine, stop_engine  # noqa: E402
from contract_store_fixture import (  # noqa: E402
    build_contract_environment,
    capture_cli_json_contracts,
    capture_mcp_tool_contracts,
    normalize_contract_payload,
    run_contract_service,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    contracts_root = ROOT / "tests" / "fixtures" / "contracts"
    cli_dir = contracts_root / "cli_json"
    mcp_dir = contracts_root / "mcp_tools"
    cli_dir.mkdir(parents=True, exist_ok=True)
    mcp_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cm-contracts-", ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        engine_dir = tmp_path / "engine"
        port = _free_port()
        engine_settings = Settings.load(engine_dir, create=True)
        engine_settings.arcade_url = f"http://127.0.0.1:{port}"
        engine_settings.save()
        dist = os.environ.get("CAIRNMIND_ARCADE_DIST")
        install_engine(engine_settings, Path(dist) if dist else None)
        ensure_engine(engine_settings)
        saved_env = {k: os.environ.get(k) for k in ("CAIRNMIND_ARCADE_URL", "CAIRNMIND_ARCADE_PASSWORD",
                                                   "CAIRNMIND_ARCADE_DATABASE")}
        try:
            os.environ["CAIRNMIND_ARCADE_URL"] = engine_settings.arcade_url
            os.environ["CAIRNMIND_ARCADE_PASSWORD"] = engine_settings.arcade_password
            os.environ["CAIRNMIND_ARCADE_DATABASE"] = "cairnmind_contract_examples"
            env = build_contract_environment(tmp_path)
            with run_contract_service(env["settings"]):
                cli_outputs = capture_cli_json_contracts(env)
                mcp_outputs = asyncio.run(capture_mcp_tool_contracts(env))
        finally:
            for key, val in saved_env.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
            stop_engine(engine_settings)

        for name, raw_payload in cli_outputs.items():
            normalized = normalize_contract_payload(raw_payload, env, command_name=name)
            (cli_dir / f"{name}.json").write_text(
                json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        for name, raw_payload in mcp_outputs.items():
            normalized = normalize_contract_payload(raw_payload, env, command_name=name)
            (mcp_dir / f"{name}.json").write_text(
                json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
    print(f"Generated {len(cli_outputs)} CLI and {len(mcp_outputs)} MCP frozen contract examples.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
