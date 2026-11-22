"""Every internal link of README.md and docs/ names a file that exists and, with an anchor, a heading that
exists (#275). Anchors follow GitHub: the heading's text in lower case, every character that is not a letter,
digit, underscore, hyphen or space dropped, spaces turned into hyphens, and -1, -2 ... added to a repeated
heading. shortcut: reference-style links, HTML links, setext headings and indented code blocks are not read;
none is used in these files (21 November 2026). Upgrade when one appears."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
LINK = re.compile(r"\[[^\]]*\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)")
EXTERNAL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")  # https:, mailto: ...


def documentation(root: Path) -> list[Path]:
    return [root / "README.md", *sorted((root / "docs").rglob("*.md"))]


def prose_lines(text: str):
    """(line number, line) outside fenced code blocks; a fence closes with the same character, at least as long."""
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        opened = FENCE.match(line)
        if fence:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = None
            continue
        if opened:
            fence = opened.group(1)
            continue
        yield number, line


def anchor(heading: str) -> str:
    """GitHub's anchor of a heading's text; a link inside a heading counts by its text."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    """Every heading anchor of a Markdown file, repeated ones with -1, -2 ..."""
    seen: dict[str, int] = {}
    found: set[str] = set()
    for _, line in prose_lines(path.read_text(encoding="utf-8")):
        match = HEADING.match(line)
        if match:
            base = anchor(match.group(2))
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else f"{base}-{count}")
    return found


def broken_links(root: Path, files: list[Path]) -> list[str]:
    """'path:line: target (no such file)' or '(no such heading)' for each broken internal link, in file and line
    order. Links in code spans and fenced blocks are not links; an anchor is checked only on a .md target; a
    target without a file part is the page itself."""
    broken: list[str] = []
    cache: dict[Path, set[str]] = {}
    for path in files:
        for number, line in prose_lines(path.read_text(encoding="utf-8")):
            for target in LINK.findall(CODE_SPAN.sub("", line)):
                if EXTERNAL.match(target):
                    continue
                name, _, fragment = target.partition("#")
                goal = (path.parent / name).resolve() if name else path
                where = f"{path.relative_to(root).as_posix()}:{number}: {target}"
                if not goal.exists():
                    broken.append(where + " (no such file)")
                    continue
                if fragment and goal.suffix == ".md":
                    if goal not in cache:
                        cache[goal] = anchors(goal)
                    if fragment not in cache[goal]:
                        broken.append(where + " (no such heading)")
    return broken


def test_every_internal_link_of_the_documentation_resolves():
    files = documentation(ROOT)
    assert len(files) > 10
    assert broken_links(ROOT, files) == []


def test_a_missing_file_and_missing_headings_are_reported(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "GUIDE.md").write_text("# Guide\n\n### The agent's own file changes\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "# Title\n"
        "\n"
        "## Quick start (2 minutes)\n"
        "\n"
        "[ok](docs/GUIDE.md#the-agents-own-file-changes) [here](#quick-start-2-minutes) [folder](docs/)"
        " [web](https://example.com/a#b)\n"
        "[gone](docs/MISSING.md) [no heading](docs/GUIDE.md#nowhere) [old anchor](#quick-start)"
        " [fenced](#not-a-heading)\n"
        "`[code](docs/IGNORED.md)`\n"
        "```bash\n"
        "# Not a heading\n"
        "[fenced link](docs/IGNORED.md)\n"
        "```\n",
        encoding="utf-8",
    )
    assert broken_links(tmp_path, documentation(tmp_path)) == [
        "README.md:6: docs/MISSING.md (no such file)",
        "README.md:6: docs/GUIDE.md#nowhere (no such heading)",
        "README.md:6: #quick-start (no such heading)",
        "README.md:6: #not-a-heading (no such heading)",
    ]
