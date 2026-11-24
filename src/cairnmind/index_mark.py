"""The mark the process that prepares the store leaves while it builds a lookup index (#422).

On a very large store the build takes minutes (store.LOOKUP_INDEX_WAIT per statement), longer than a command that
starts the service waits. The mark is one file of the data directory holding that process's pid and, on a second
line, its start identity, read by upgrade_mark.mark_state like the other two marks; the process removes it when the
build ends or fails. Nothing is written when no index is to be built. A mark whose process is gone, or whose pid now
belongs to a process with another start, is ignored and removed.
The English sentence is the source; MESSAGES is its Polish for the terminal catalog.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import upgrade_mark

MARK = "index-build-in-progress"

NOTE_RUNNING = ("The store is being prepared for this version: a lookup index is being built (process {pid}). It is "
                "usually the first start after an upgrade, can take minutes on a large store, and nothing is to be done.")
BRIDGE_INDEX = "{reason} Call the tool again shortly."  # what the MCP bridge answers during the build (#425)
MESSAGES = {
    NOTE_RUNNING: ("Baza jest przygotowywana do tej wersji: trwa budowa indeksu wyszukiwania (proces {pid}). To "
                   "zwykle pierwszy start po aktualizacji, na dużej bazie może potrwać kilka minut i nic nie trzeba robić."),
    BRIDGE_INDEX: "{reason} Wywołaj narzędzie ponownie za chwilę.",
}


def mark_path(data_dir) -> Path:
    return Path(data_dir) / MARK


@contextmanager
def building(data_dir):
    """Hold the mark for the block that builds the indexes. The mark only informs: a data directory that cannot
    take it does not stop the build."""
    with ExitStack() as stack:
        try:
            stack.enter_context(upgrade_mark.own_mark(mark_path(data_dir)))
        except OSError:
            pass
        yield


def state(data_dir) -> tuple[str, int | None]:
    # The builder runs as `python -m cairnmind` or the `cairnmind` script: an argument that is that name, not one that
    # merely contains it (a pager on service.log). A pid reused by a process with another start is not the holder.
    return upgrade_mark.mark_state(mark_path(data_dir), lambda arguments: any(Path(part).name == "cairnmind" for part in arguments),
                                   lambda program: Path(program).name.lower().startswith("python"))


def note(data_dir) -> str | None:
    """The sentence while a living process builds an index; a stale mark is removed and says nothing, and one that
    cannot be read is ignored (the command then waits as it always did)."""
    try:
        read = mark_path(data_dir).read_bytes()
    except OSError:
        read = None
    kind, pid = state(data_dir)
    if kind == "stale":
        try:  # only the mark that was judged: a builder that wrote a new one meanwhile keeps it
            if read is not None and mark_path(data_dir).read_bytes() == read:
                mark_path(data_dir).unlink()
        except OSError:
            pass
    return NOTE_RUNNING.format(pid=pid) if kind == "running" else None
