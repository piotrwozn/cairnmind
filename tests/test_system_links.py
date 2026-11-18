"""Links of the system (/tmp, /var on macOS) are accepted; a link a user could plant is still refused (#229).

Linux has no such links, so each test builds a fake one: `<tmp>/systmp -> <tmp>/private`, entered in the
platform's table, with the root-ownership check replaced (a test cannot own a link as root).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from cairnmind import system_links
from cairnmind.memory import _check_manifest
from cairnmind.memory_paths import bound_metadata, is_memory_path, memory_bindings
from cairnmind.system_links import (SYSTEM_LINKS, owned_by_system, passes_planted_link, platform_links,
                                    system_spelling, verified_links)
from cairnmind.terminal_preview import preview_settings


def fake_system_link(tmp_path: Path, monkeypatch, *, platform: str | None = None) -> tuple[Path, Path]:
    """`<tmp>/systmp -> <tmp>/private`, a system link of `platform` (the running one by default)."""
    target, link = tmp_path / "private", tmp_path / "systmp"
    target.mkdir()
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(system_links, "SYSTEM_LINKS", {platform or sys.platform: ((str(link), str(target)),)})
    monkeypatch.setattr(system_links, "owned_by_system", lambda path: path == link)
    # The real lookup, uncached, so that the fake link never stays in the cache of the process.
    links = platform_links.__wrapped__()
    monkeypatch.setattr(system_links, "platform_links", lambda: links)
    return link, target


def test_the_darwin_table_names_tmp_var_and_etc_and_linux_has_none():
    assert dict(SYSTEM_LINKS["darwin"]) == {"/tmp": "/private/tmp", "/var": "/private/var", "/etc": "/private/etc"}
    assert "linux" not in SYSTEM_LINKS
    if sys.platform == "linux":
        assert platform_links() == ()
        assert system_spelling("/tmp/x") == Path("/tmp/x")


def test_the_darwin_path_is_selected_by_the_platform(tmp_path, monkeypatch):
    link, target = fake_system_link(tmp_path, monkeypatch, platform="darwin")
    if sys.platform != "darwin":
        assert system_links.platform_links() == ()  # The table of another platform is never used.
    with monkeypatch.context() as patch:
        patch.setattr(system_links.sys, "platform", "darwin")
        assert platform_links.__wrapped__() == ((link, target),)
    assert system_spelling(link / "a" / "b", ((link, target),)) == target / "a" / "b"


def test_a_link_owned_by_the_user_is_not_the_systems(tmp_path):
    target, link = tmp_path / "private", tmp_path / "systmp"
    target.mkdir()
    link.symlink_to(target, target_is_directory=True)
    if os.getuid() != 0:
        assert not owned_by_system(link)
        assert verified_links([(link, target)]) == ()  # The real ownership check: this link is the user's.
    assert not owned_by_system(target)  # Not a link at all.


def test_an_entry_that_fails_a_check_is_left_out(tmp_path):
    target, link = tmp_path / "private", tmp_path / "systmp"
    target.mkdir()
    (tmp_path / "other").mkdir()
    link.symlink_to(target, target_is_directory=True)
    (tmp_path / "linked-target").symlink_to(target, target_is_directory=True)
    always = lambda path: True  # noqa: E731
    assert verified_links([(link, target)], always) == ((link, target),)
    assert verified_links([(link, target)], lambda path: False) == ()
    assert verified_links([(link, tmp_path / "other")], always) == ()  # It leads somewhere else.
    assert verified_links([(link, tmp_path / "linked-target")], always) == ()  # A target that is a link.
    assert verified_links([(tmp_path / "missing", target)], always) == ()
    assert verified_links([("relative", target)], always) == ()
    hop, chained = tmp_path / "hop", tmp_path / "chained"
    hop.symlink_to(target, target_is_directory=True)
    chained.symlink_to(hop, target_is_directory=True)
    assert verified_links([(chained, target)], always) == ()  # Reaches it through another link.
    relative = tmp_path / "relative-link"
    relative.symlink_to("private", target_is_directory=True)  # As macOS spells /tmp -> private/tmp.
    assert verified_links([(relative, target)], always) == ((relative, target),)


def test_only_the_start_of_a_path_is_replaced_and_a_planted_link_is_still_refused(tmp_path, monkeypatch):
    link, target = fake_system_link(tmp_path, monkeypatch)
    (target / "real").mkdir()
    (target / "planted").symlink_to(target / "real", target_is_directory=True)
    (tmp_path / "mine").symlink_to(target, target_is_directory=True)  # Leads to the same place, not in the table.
    assert not passes_planted_link(link / "real" / "file")
    assert not passes_planted_link(target / "real" / "file")
    assert passes_planted_link(link / "planted" / "file")
    assert passes_planted_link(tmp_path / "mine" / "real")
    assert passes_planted_link(link / "real", links=())  # Without the table, as before #229.
    assert system_spelling("relative/systmp") == Path("relative/systmp")


def manifest_at(folder: Path) -> Path:
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({"format": "not-a-package"}))
    return folder


def test_a_memory_package_under_a_system_link_is_read_and_a_planted_link_is_refused(tmp_path, monkeypatch):
    link, target = fake_system_link(tmp_path, monkeypatch)
    manifest_at(target / "package")
    (target / "planted").symlink_to(target / "package", target_is_directory=True)
    (tmp_path / "mine").symlink_to(target / "package", target_is_directory=True)
    # Past the link check: the manifest itself is what is refused.
    with pytest.raises(ValueError, match="Unsupported portable memory format"):
        _check_manifest(link / "package")
    for planted in (target / "planted", link / "planted", tmp_path / "mine"):
        with pytest.raises(ValueError, match="cannot traverse symbolic links"):
            _check_manifest(planted)


def test_the_preview_data_directory_may_be_under_a_system_link(tmp_path, monkeypatch):
    link, target = fake_system_link(tmp_path, monkeypatch)
    settings = preview_settings(link / "preview", create=True)
    assert settings.data_dir == target / "preview" and (target / "preview" / "terminal-preview.json").is_file()
    (target / "planted").symlink_to(target / "elsewhere", target_is_directory=True)
    (target / "elsewhere").mkdir()
    with pytest.raises(ValueError, match="symlink"):
        preview_settings(link / "planted" / "preview", create=True)


def test_reserved_memory_is_matched_through_a_system_link_on_either_side(tmp_path, monkeypatch):
    link, target = fake_system_link(tmp_path, monkeypatch)
    memory = Path(".memory/cairnmind/manifest.json")
    assert is_memory_path(link / "project" / memory, target / "project")
    assert is_memory_path(target / "project" / memory, link / "project")
    assert not is_memory_path(link / "project" / "app.py", target / "project")


class Bindings:
    """The two queries memory_bindings makes, answered from lists."""

    def __init__(self, roots, rows):
        self.roots, self.rows = roots, rows

    def query(self, sql, params=None):
        if "project_roots" in sql:
            return [{"path": str(root)} for root in self.roots]
        return [{"origin_root": str(origin), "local_root": str(local)} for origin, local in self.rows]


def test_recalled_paths_map_when_origin_and_record_spell_a_system_link_differently(tmp_path, monkeypatch):
    link, target = fake_system_link(tmp_path, monkeypatch)
    local = tmp_path / "computer-b" / "project"
    local.mkdir(parents=True)
    for origin, recorded in ((link / "a" / "project", target / "a" / "project"),
                             (target / "a" / "project", link / "a" / "project")):
        store = Bindings([local], [(origin, local)])
        mapped = bound_metadata(store, "p", {"path": str(recorded / "README.md"), "root": str(recorded)})
        assert mapped == {"path": str(local / "README.md"), "root": str(local)}
    # A path outside the origin keeps its own spelling.
    store = Bindings([local], [(link / "a" / "project", local)])
    assert bound_metadata(store, "p", {"path": str(link / "b" / "x.py")}) == {"path": str(link / "b" / "x.py")}
    assert memory_bindings(store, "p") == [(target / "a" / "project", local)]
