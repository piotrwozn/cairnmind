"""Whether a project's code graph is built: the one place that decides it (#261, #286).

A snapshot does not carry the code graph; indexing builds it from the project's files and then writes the
project's graph_fingerprint. Until then the import, `memory status` and the readers of the graph (ask, impact)
say so.
"""
from __future__ import annotations

CODE_GRAPH_NOT_BUILT = ("The code graph of this project is not built yet: indexing builds it from the project's "
                        "files, so what is found about code can be empty or partial.")


def code_graph_pending(project: dict | None) -> bool:
    """True while indexing has not built the graph of this project row."""
    return not (project or {}).get("graph_fingerprint")
