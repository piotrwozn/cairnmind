"""The code graph of a change to some files is computed from those files, and is the graph of a computation of all (#409).

`_graph_parts` is given the parts of the last graph and the files as they are now: it computes again the files
whose source changed and those that import a file whose symbols changed. After every kind of edit the result is
compared, part by part, with a computation of every file from nothing. No database is involved.
"""
from __future__ import annotations

import ast
import hashlib
import random
from pathlib import Path

import pytest

from cairnmind import ingest
from cairnmind.parsing import analyze_code


def write(root: Path, name: str, text: str) -> None:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(text)


@pytest.fixture
def project(tmp_path):
    root = tmp_path.resolve()
    write(root, "a.py", "def helper(x):\n    return x\n\n\nclass Box:\n    def open(self):\n        return helper(1)\n")
    write(root, "b.py", "import x\nfrom a import helper, Box\n\n\ndef use():\n    return helper(2) + Box().open()\n")
    write(root, "c.py", "import a\n\n\ndef run():\n    return a.helper(3)\n")
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/d.py", "from .e import thing\n\n\ndef again():\n    return thing()\n")
    write(root, "pkg/e.py", "def thing():\n    return 1\n")
    write(root, "test_b.py", "from b import use\n\n\ndef test_use():\n    assert use()\n")
    return root


def files_of(root: Path) -> dict[Path, dict]:
    indexed = {}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text()
        source = {"id": hashlib.sha256(f"{path}{text}".encode()).hexdigest()[:16], "uri": str(path), "kind": "code"}
        indexed[path] = {"source": source, "analysis": analyze_code(path, text), "root": root}
    return indexed


STEPS = {
    "edit a body, every line where it was": lambda root: write(
        root, "a.py", "def helper(x):\n    return x + 1\n\n\nclass Box:\n    def open(self):\n        return helper(1)\n"),
    "add a line before the symbols of a file others import": lambda root: write(
        root, "a.py", "# a note\ndef helper(x):\n    return x\n\n\nclass Box:\n    def open(self):\n        return helper(1)\n"),
    "rename a function that other files call": lambda root: write(
        root, "a.py", "def helper2(x):\n    return x\n\n\nclass Box:\n    def open(self):\n        return helper2(1)\n"),
    "move a symbol to another file": lambda root: (
        write(root, "a.py", "class Box:\n    def open(self):\n        return 1\n"),
        write(root, "pkg/e.py", "def thing():\n    return 1\n\n\ndef helper(x):\n    return x\n")),
    "change an import": lambda root: write(
        root, "b.py", "import x\nfrom pkg.e import helper, thing\n\n\ndef use():\n    return helper(2) + thing()\n"),
    "edit a file that nobody imports": lambda root: write(
        root, "test_b.py", "from b import use\n\n\ndef test_use():\n    assert use() == 1\n"),
    "add a file that an import was looking for": lambda root: write(root, "x.py", "def other():\n    return 0\n"),
    "add a file": lambda root: write(root, "pkg/f.py", "from a import Box\n\n\ndef make():\n    return Box()\n"),
    "delete a file": lambda root: (root / "c.py").unlink(),
    "delete a file that others import": lambda root: (root / "x.py").unlink(),
}


def rows_of(parts: dict) -> tuple:
    return ingest._union(parts)


def test_after_every_kind_of_edit_the_parts_are_those_of_a_computation_of_every_file(project):
    indexed = files_of(project)
    parts, nodes, edges = ingest._graph_parts("p", indexed, {})
    assert len(rows_of(parts)[0]) == len(nodes) > 20 and len(rows_of(parts)[1]) == len(edges) > 20
    for step, change in STEPS.items():
        change(project)
        indexed = files_of(project)
        reuse = parts if parts.keys() == indexed.keys() else {}  # the condition of `_build_graph`
        parts, nodes, edges = ingest._graph_parts("p", indexed, reuse)
        every_part, every_node, every_edge = ingest._graph_parts("p", indexed, {})
        assert parts == every_part, step
        assert rows_of(parts) == rows_of(every_part), step
        # What was computed is the rows of the full computation.
        assert all(every_node[identifier] == item for identifier, item in nodes.items()), step
        assert all(every_edge[identifier] == item for identifier, item in edges.items()), step


def test_a_change_computes_the_files_that_it_changes_and_those_that_import_changed_symbols_only(project):
    def computed(change):
        before = files_of(project)
        parts, *_ = ingest._graph_parts("p", before, {})
        change(project)
        after = files_of(project)
        _, nodes, edges = ingest._graph_parts("p", after, parts)
        names = {entry["source"]["id"]: path.name for path, entry in after.items()}
        return {names[item["source_id"]] for item in (*nodes.values(), *edges.values()) if item["source_id"]}, len(nodes)

    every = len(ingest._graph_parts("p", files_of(project), {})[1])
    # The body of a function: its file only, though a.py is imported by b.py and c.py.
    files, rows = computed(STEPS["edit a body, every line where it was"])
    assert files == {"a.py"} and rows < every / 4
    # A line before its symbols: the files that import them look at new symbols, test_b.py imports b.py, not a.py.
    files, _ = computed(STEPS["add a line before the symbols of a file others import"])
    assert files == {"a.py", "b.py", "c.py"}
    files, _ = computed(STEPS["edit a file that nobody imports"])
    assert files == {"test_b.py"}


@pytest.mark.parametrize("before, after", [("x = 1; x = 2\n", "x = 1\n"), ("x = 1\n", "x = 1; x = 2\n"),
                                           ("export function f(){} export function f(){}\n", "export function f(){}\n")])
def test_a_file_whose_symbols_keep_their_ids_but_not_their_number_is_computed_for_its_importers(tmp_path, before, after):
    # Two symbols of one name on one line have one id, and make a call to the name ambiguous; one does not.
    root, suffix = tmp_path.resolve(), "js" if "function" in before else "py"
    write(root, f"dup.{suffix}", before)
    write(root, f"user.{suffix}", "from dup import x\n\n\ndef run():\n    return x()\n" if suffix == "py"
          else "import { f } from './dup';\nexport function run() { return f(); }\n")
    parts, *_ = ingest._graph_parts("p", files_of_any(root), {})
    write(root, f"dup.{suffix}", after)
    indexed = files_of_any(root)
    again, *_ = ingest._graph_parts("p", indexed, parts)
    assert again == ingest._graph_parts("p", indexed, {})[0]


def files_of_any(root: Path) -> dict[Path, dict]:
    indexed = {}
    for path in sorted(p for p in root.iterdir() if p.suffix in {".py", ".js"} and not p.is_symlink()):  # as the walk
        text = path.read_text()
        source = {"id": hashlib.sha256(f"{path}{text}".encode()).hexdigest()[:16], "uri": str(path), "kind": "code"}
        indexed[path] = {"source": source, "analysis": analyze_code(path, text), "root": root}
    return indexed


def seeded_project(root: Path) -> None:
    """Twelve modules: each defines a constant and two functions, and imports and calls those of the two before it."""
    for number in range(12):
        lines = []
        for back in (1, 2):
            if number >= back:
                other = number - back
                lines.append(f"from m{other} import V{other}, f{other}")
        lines += ["", "", f"V{number} = {number}", "", "", f"def f{number}(x):", "    return x", "", "",
                  f"def g{number}():", f"    return f{number}(1)"]
        for back in (1, 2):
            if number >= back:
                lines += ["", "", f"def use{back}():", f"    return f{number - back}(2) + V{number - back}()"]
        write(root, f"m{number}.py", "\n".join(lines) + "\n")


def random_edit(root: Path, rng: random.Random, counter: int) -> str:
    """One of: delete, duplicate or swap functions, add or remove an import, rename a symbol at its definition only,
    put a second constant of the same name on a line (two symbols of one id) or take it away."""
    paths = sorted(root.glob("m*.py"))
    while True:
        path, kind = rng.choice(paths), rng.choice(["delete", "duplicate", "swap", "add import", "remove import",
                                                    "rename", "twin", "untwin"])
        text = path.read_text()
        lines = text.split("\n")
        tree = ast.parse(text)
        found = [(n.name, n.lineno, n.end_lineno) for n in tree.body if isinstance(n, ast.FunctionDef)]
        if kind in ("delete", "duplicate", "rename") and found:
            name, start, end = rng.choice(found)
            if kind == "delete":
                lines = lines[:start - 1] + lines[end:]
            elif kind == "duplicate":
                lines = lines[:end] + ["", ""] + lines[start - 1:end] + lines[end:]
            else:
                lines[start - 1] = lines[start - 1].replace(f"def {name}(", f"def {name}_r(")
        elif kind == "swap" and len(found) >= 2:
            (_, s1, e1), (_, s2, e2) = sorted(rng.sample(found, 2), key=lambda item: item[1])
            lines = lines[:s1 - 1] + lines[s2 - 1:e2] + lines[e1:s2 - 1] + lines[s1 - 1:e1] + lines[e2:]
        elif kind == "add import":
            other = rng.choice([p for p in paths if p != path]).stem
            number = other[1:]
            lines += ["", f"from {other} import f{number}", "", "", f"def added{counter}():", f"    return f{number}(3)", ""]
        elif kind == "remove import":
            imports = [n.lineno for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
            if not imports:
                continue
            at = rng.choice(imports)
            lines = lines[:at - 1] + lines[at:]
        elif kind == "twin" and any(line.startswith("V") and ";" not in line for line in lines):
            lines = [f"{line}; {line}" if line.startswith("V") and ";" not in line else line for line in lines]
        elif kind == "untwin" and "; V" in text:
            lines = [line.split("; ")[0] for line in lines]
        else:
            continue
        path.write_text("\n".join(lines))
        return f"{kind} in {path.name}"


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_forty_seeded_edits_leave_the_parts_of_a_computation_of_every_file(tmp_path, seed):
    root, rng = tmp_path.resolve(), random.Random(seed)
    seeded_project(root)
    indexed = files_of(root)
    parts, *_ = ingest._graph_parts("p", indexed, {})
    twins = reused = 0
    for step in range(40):
        done = random_edit(root, rng, step)
        twins += done.startswith(("twin", "untwin"))
        indexed = files_of(root)
        reuse = parts if parts.keys() == indexed.keys() else {}  # the condition of `_build_graph`
        reused += bool(reuse)
        parts, nodes, edges = ingest._graph_parts("p", indexed, reuse)
        every_part, every_node, every_edge = ingest._graph_parts("p", indexed, {})
        assert parts == every_part, (step, done)
        digests, edge_ids = ingest._union(parts)
        # The ids as a multiset, counted part by part, and the rows that were computed against those of every file.
        assert sorted(i for p in parts.values() for d in (p["first"][0], p["second"][0]) for i in d) == sorted(
            i for p in every_part.values() for d in (p["first"][0], p["second"][0]) for i in d), (step, done)
        assert (digests, edge_ids) == ingest._union(every_part), (step, done)
        assert all(every_node[i] == n for i, n in nodes.items()) and all(every_edge[i] == e for i, e in edges.items())
    assert reused == 40 and twins >= 1


def test_an_import_through_a_symlink_that_now_leads_elsewhere_is_resolved_again(tmp_path):
    # The walk leaves a symlink out, but resolving an import follows it: the importer's file did not change.
    root = tmp_path.resolve()
    write(root, "one.py", "def helper():\n    return 1\n")
    write(root, "two.py", "def helper():\n    return 2\n")
    (root / "link.py").symlink_to("one.py")
    write(root, "user.py", "from link import helper\n\n\ndef go():\n    helper()\n")
    parts, *_ = ingest._graph_parts("p", files_of_any(root), {})
    (root / "link.py").unlink()
    (root / "link.py").symlink_to("two.py")
    indexed = files_of_any(root)
    again, *_ = ingest._graph_parts("p", indexed, parts)
    assert again == ingest._graph_parts("p", indexed, {})[0]


def test_other_files_or_another_root_are_computed_again_in_full(project):
    indexed = files_of(project)
    parts, *_ = ingest._graph_parts("p", indexed, {})
    other = {path: {**entry, "root": project / "pkg"} for path, entry in indexed.items()}
    again, nodes, _ = ingest._graph_parts("p", other, parts)  # the root a file was reached through steers its imports
    assert again == ingest._graph_parts("p", other, {})[0] and len(nodes) == len(ingest._graph_parts("p", other, {})[1])
