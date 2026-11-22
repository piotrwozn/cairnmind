"""The README's quick start is the whole first start: every command exists, and the first memory operation comes
before anything that needs a coding client (#287)."""
from pathlib import Path
import re

from test_deployment import cli_commands, unknown_commands

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def quick_start() -> str:
    start = README.index("## Quick start")
    return README[start:README.index("\n## ", start + 1)]


def test_every_cairnmind_command_in_the_readme_exists():
    assert len(re.findall(r"^cairnmind [a-z]", README, re.M)) >= 6
    assert unknown_commands(README, cli_commands()) == []


def test_the_quick_start_remembers_and_reads_back_before_it_connects_a_client():
    section = quick_start()
    block = section[section.index("```bash"):section.index("```", section.index("```bash") + 3)]
    order = [block.index(command) for command in ("cairnmind project add", "cairnmind remember", "cairnmind brief",
                                                   "cairnmind ask")]
    # The installer ends with the check, so the block has no doctor step of its own (#313); the text below names it.
    assert order == sorted(order) and "cairnmind doctor" not in block
    assert "cairnmind install" not in block and "cairnmind run" not in block
    assert "`cairnmind doctor` repeats the installer's check" in section
    assert block.index("install-wsl.sh") < order[0]
    assert section.index("### Connect Codex or Claude Code") > section.index("cairnmind ask")


def test_the_quick_start_says_what_is_required_what_is_optional_and_which_files_install_writes():
    section = quick_start()
    required = section[section.index("**What you need**"):section.index("**What gets installed:**")]
    assert "Java 21" in required and "Docker" in required and "sudo apt install -y openjdk-21-jre-headless" in required
    assert "no Ollama, no models, no GPU, no tmux and no coding assistant are needed" in required
    assert "wsl --install -d Ubuntu" in section and "curl -LsSf https://astral.sh/uv/install.sh | sh" in section
    for name in ("AGENTS.md", "CLAUDE.md", ".mcp.json", ".claude/settings.local.json", ".codex/config.toml",
                 ".codex/hooks.json"):
        assert name in section, name
    assert "Ctrl+B then i" in README  # where a proposed finding is confirmed
