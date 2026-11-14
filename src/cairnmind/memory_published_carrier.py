"""Where a project's published memory goes: its carrier (#173).

The package is the same whatever carries it; a carrier only chooses the directory it is written to.

- repository (the default): `<project root>/.memory/cairnmind-published/`, reviewed beside the code.
- checkout: `<a checkout of a memory repository>/<publication identity>/`, for a team that keeps memory
  out of the code repository. The person commits and pushes it; Cairnmind never stages, commits or pushes.

A server carrier needs a receiving Cairnmind service with its own user and TLS (#72); it is not offered
until that exists. Changing the carrier moves nothing: the result says where the earlier package is and
what to do with it. A checkout path is checked again every time it is used, since it can change.
"""
from __future__ import annotations

import os
from pathlib import Path

from .memory_paths import PUBLISHED_RELATIVE, RESERVED_DIRECTORIES
from .memory_published_format import canonical_uuid
from .sql import insert_document, one_row, update_document, utc_now
from .terminal_safe import safe_text

CARRIERS = ("repository", "checkout")
SETTING = "publication_carrier"
SETTING_PREVIOUS = "publication_carrier_previous"
DEFAULT = {"kind": "repository"}
_MAX_PATH = 4096


def carrier_of(reader, project_id):
    """The project's carrier as stored, or the default."""
    row = one_row(reader, "SELECT FROM memory_settings WHERE id=:id", {"id": project_id}, False) or {}
    value = row.get(SETTING)
    return dict(value) if isinstance(value, dict) and value.get("kind") in CARRIERS else dict(DEFAULT)


def identity_of(project):
    """The directory name of a package in a checkout: the team's publication identity, or the project's own ID
    until its first publication binds it."""
    return project.get("publication_id") or project["id"]


def checkout_path(store, project_id, raw):
    """A checkout path, validated: absolute, a real directory in a Git checkout, with no symbolic link on the way,
    and not inside Cairnmind's data, a registered project's files or any project's memory."""
    if (not isinstance(raw, str) or not raw or len(raw) > _MAX_PATH or safe_text(raw) != raw
            or any(ord(c) < 0x20 or c in "  " for c in raw)):
        raise ValueError("A checkout path must be a plain absolute path")
    given = Path(raw).expanduser()
    if not given.is_absolute():
        raise ValueError(f"A checkout path must be absolute: {raw}")
    path = Path(os.path.normpath(given))
    if Path(os.path.realpath(path)) != path:
        raise ValueError(f"The checkout path {path} is or passes through a symbolic link; give the real directory")
    data_dir = Path(os.path.realpath(store.data_dir))
    if path == data_dir or path.is_relative_to(data_dir) or data_dir.is_relative_to(path):
        raise ValueError(f"The checkout path {path} overlaps Cairnmind's own data directory")
    names = {row["id"]: row["name"] for row in store.query("SELECT id,name FROM projects")}
    for row in store.query("SELECT project_id,path FROM project_roots"):
        root = Path(row["path"])
        name = names.get(row["project_id"], row["project_id"])
        for reserved in RESERVED_DIRECTORIES:
            if path.is_relative_to(root / reserved):
                raise ValueError(f"The checkout path {path} is inside the memory of project {name} ({root / reserved})")
        if path.is_relative_to(root):
            if row["project_id"] == project_id:
                raise ValueError(f"The checkout path {path} is inside this project's own files; "
                                 "the repository carrier (the default) writes there")
            raise ValueError(f"The checkout path {path} is inside the files of project {name}, "
                             "which would index the package as its code")
        top = _git_top(root)
        if root.is_relative_to(path) or (top is not None and top == _git_top(path)):
            # A package beside a project in its own repository sits outside every reserved directory: whoever
            # registers that repository's top level would index and fingerprint it as code (#174).
            raise ValueError(f"The checkout path {path} shares a Git repository with project {name} ({root}); "
                             "use the repository carrier (the default) there, or a checkout of a separate repository")
    if not path.is_dir():
        raise ValueError(f"The checkout path {path} is not an existing directory")
    if _git_top(path) is None:
        raise ValueError(f"The checkout path {path} is not in a Git checkout; clone the memory repository first")
    return path


def _git_top(path):
    """The nearest directory at or above path holding .git (read from the file system, no git command)."""
    path = Path(path)
    return next((folder for folder in (path, *path.parents) if (folder / ".git").exists()), None)


def destination(store, project, root, carrier=None):
    """(carrier, directory) where the package of this project is written and read."""
    carrier = carrier or carrier_of(store, project["id"])
    if carrier["kind"] == "repository":
        return carrier, Path(root) / PUBLISHED_RELATIVE
    checkout = checkout_path(store, project["id"], carrier.get("path"))
    return carrier, checkout / identity_of(project)


def packages_in(checkout, *, besides=None):
    """The publication identities of packages in a checkout (directories named by a UUID with a FORMAT.json)."""
    found = []
    for entry in sorted(Path(checkout).iterdir()):
        if (entry.name != besides and canonical_uuid(entry.name) and not entry.is_symlink() and entry.is_dir()
                and (entry / "FORMAT.json").is_file()):
            found.append(entry.name)
    return found


def unbound_in_checkout(store, project, folder, existing, new_package):
    """A project with no publication identity yet, publishing into a checkout that holds other packages, is
    refused: a teammate's package is probably there, and publishing would start a second one silently. In the
    repository the same case is refused by FORMAT.json ("belongs to X; import it first")."""
    from .memory import MemoryConflict
    if (new_package or project.get("publication_id") or "FORMAT.json" in existing
            or carrier_of(store, project["id"])["kind"] != "checkout"):
        return
    others = packages_in(folder.parent, besides=folder.name)
    if others:
        listed = ", ".join(others)
        raise MemoryConflict(
            f"The checkout {folder.parent} holds published memory of {listed} and this project has none yet. Import "
            f"this project's package with `cairnmind memory import-published {folder.parent / others[0]} --project "
            f"{project['name']}`, which binds it, or start a new package here with `cairnmind memory publish "
            f"{project['name']} --new-package`")


def carrier_status(store, project, root):
    """`memory status` → published: the carrier, and a package left at the repository place when it is not used."""
    carrier = carrier_of(store, project["id"])
    result = {"carrier": carrier}
    in_repository = Path(root) / PUBLISHED_RELATIVE
    if carrier["kind"] != "repository":
        result["other_package"] = package_at(in_repository)
    # The carrier before the last change, and any package still there, so status keeps naming it after the
    # one-off message of `memory carrier` scrolled away.
    row = one_row(store, "SELECT FROM memory_settings WHERE id=:id", {"id": project["id"]}, False) or {}
    previous = row.get(SETTING_PREVIOUS)
    if isinstance(previous, dict) and previous.get("kind") in CARRIERS:
        result["previous_carrier"] = dict(previous)
        try:
            folder = destination(store, project, root, dict(previous))[1]
        except ValueError as exc:  # The earlier checkout moved or became a link.
            result["previous_package"] = {"error": str(exc)}
        else:
            # Already reported as other_package, or the same place as now: nothing more to say.
            repeated = folder == in_repository and "other_package" in result
            same = carrier["kind"] == previous["kind"] == "repository" or previous == carrier
            result["previous_package"] = None if repeated or same else package_at(folder)
    return result


def package_at(folder):
    """{destination, files} of a package left at a directory, or None when nothing is there."""
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        return None
    files = sum(1 for path in folder.rglob("*") if path.is_file())
    return {"destination": str(folder), "files": files} if files else None


def configure(store, project_id, kind, path=None, worktree=None):
    """Choose the carrier. Nothing is moved; the result says where the earlier package is and what to do."""
    from .memory import MemoryManager
    if kind == "server":
        raise ValueError("The server carrier needs a receiving Cairnmind service with its own user and TLS (#72); "
                         "it is not available yet")
    if kind not in CARRIERS:
        raise ValueError(f"Unknown carrier {kind!r}; use repository or checkout")
    if kind == "repository" and path is not None:
        raise ValueError("The repository carrier takes no path")
    project, root = MemoryManager(store)._scope(project_id, worktree)
    new = {"kind": "repository"} if kind == "repository" else {
        "kind": "checkout", "path": str(checkout_path(store, project_id, path))}
    old = carrier_of(store, project_id)
    try:
        _, earlier_folder = destination(store, project, root, old)
    except ValueError as exc:
        earlier_folder, earlier_error = None, str(exc)
    else:
        earlier_error = None
    _, folder = destination(store, project, root, new)
    if new != old:
        with store.transaction() as tx:
            if tx.query("SELECT id FROM memory_settings WHERE id=:id", {"id": project_id}):
                update_document(tx, "memory_settings", {SETTING: new, SETTING_PREVIOUS: old, "updated_at": utc_now()},
                                "id=:id", {"id": project_id})
            else:
                insert_document(tx, "memory_settings", {
                    "id": project_id, "project_id": project_id, "enabled": False, "interval_minutes": 15,
                    "worktree": str(root), SETTING: new, SETTING_PREVIOUS: old, "updated_at": utc_now()})
    earlier = package_at(earlier_folder) if earlier_folder is not None and earlier_folder != folder else None
    result = {"status": "configured" if new != old else "unchanged", "carrier": new, "destination": str(folder),
              "previous_carrier": old, "earlier_package": earlier, "already_there": package_at(folder),
              "moved": [], "git": "nothing was moved, staged, committed or pushed"}
    name = project["name"]
    if earlier:
        result["message"] = (
            f"The earlier published memory stays at {earlier['destination']} ({earlier['files']} files); nothing was "
            f"moved. If teammates published there, import it first with `cairnmind memory import-published "
            f"{earlier['destination']} --project {name}`. Then `cairnmind memory publish {name}` writes the package at "
            f"{folder}. Remove the earlier files in a commit of your own once the team reads from the new place.")
    elif earlier_error:
        result["message"] = f"The earlier carrier cannot be read ({earlier_error}); nothing was moved."
    else:
        result["message"] = (f"No published memory was at the earlier place; `cairnmind memory publish {name}` "
                             f"writes the package at {folder}.")
    if kind == "checkout":
        others = packages_in(folder.parent, besides=folder.name)
        if others and not project.get("publication_id"):
            result["message"] += (f" This checkout already holds published memory of {', '.join(others)}; if one of "
                                  f"them is this project's, import it before publishing.")
        result["message"] += " Committing and pushing the checkout is left to you."
    return result
