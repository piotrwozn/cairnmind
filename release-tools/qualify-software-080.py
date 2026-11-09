#!/usr/bin/env python3
"""One fresh qualification on private 3.13/Temurin C2/tmux 3.8; no production state."""
from pathlib import Path
import importlib.util
import json
import subprocess
import sys

BASE = Path.home() / ".cache/devbrain-runtime-080"
ROOT = BASE / "software313-tmux38"
PROJECT = Path("/mnt/c/Users/piotr/OneDrive/Dokumenty/ChatGPT/devbrain")


def main():
    if ROOT.exists():
        raise RuntimeError("Qualification already exists; do not silently repeat")
    for name in ("tmux38-teardown.json", "ssl-standalone31318.json", "imports-31318-bounded1.json"):
        report = json.loads((BASE / "evidence" / name).read_text())
        if report["status"] != "pass":
            raise RuntimeError("Candidate prerequisites failed: " + name)
    ROOT.mkdir()
    (ROOT / "evidence").mkdir()
    (ROOT / "runtimes").symlink_to(BASE / "runtimes", target_is_directory=True)
    uv = "/home/piotr/.local/bin/uv"
    python = BASE / "python313/runtime/python/bin/python3.13"
    subprocess.run([uv, "venv", "--python", str(python), str(ROOT / "venv")], check=True, timeout=30)
    subprocess.run([uv, "pip", "install", "--python", str(ROOT / "venv/bin/python"),
                    "--only-binary", ":all:", "-r", str(BASE / "requirements.txt")], check=True, timeout=120)
    spec = importlib.util.spec_from_file_location("diagnostics", Path(__file__).with_name("diagnose-runtime-080.py"))
    diagnostics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostics)
    return diagnostics.java_tests(ROOT, PROJECT, repetitions=3, tmux=BASE / "runtimes/tmux-3.8/bin/tmux")


if __name__ == "__main__":
    sys.exit(main())
