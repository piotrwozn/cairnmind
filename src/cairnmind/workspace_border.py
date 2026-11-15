"""The words on a pane's border, in the panels' language (#208).

A pane keeps its internal values in tmux options: @cairnmind_role identifies the pane, @cairnmind_control
is written by the terminal bridge and @cairnmind_memory picks the badge colour. The border never prints
them; its format compares each with the values below and prints the label in the language of the format,
which the workspace sets again when the language changes. A value the format does not know prints nothing.
"""
from __future__ import annotations

from rich.cells import cell_len

ROLE_LABELS = {"sidebar": "Projects", "client1": "Client 1", "client2": "Client 2", "inspector": "Inspector",
               "graph": "Graph", "activity": "Activity"}
CONTROL_LABELS = {"controller": "In control", "readonly": "Read only", "offline": "Offline"}
MEMORY_LABELS = {"ack": "ACK ready", "ACK required": "ACK needed", "partial": "Partial",
                 "unavailable": "Unavailable", "stale": "Stale"}


def _literal(text):
    """Catalog text as a literal branch of a tmux conditional: no format, no branch separator."""
    from .workspace import _hint
    return _hint(text)


def _pick(option, labels, language, prefix=""):
    """`#{?#{==:#{@option},value},label,...}`: the label of the option's exact value, else nothing."""
    from .terminal_i18n import tr
    result = ""
    for value, label in reversed(labels.items()):
        shown = prefix + _literal(tr(label, language))
        result = "#{?#{==:#{@cairnmind_" + option + "}," + value + "}," + shown + "," + result + "}"
    return result


def role_label(language="en"):
    return _pick("role", ROLE_LABELS, language)


def control_label(language="en"):
    return _pick("control", CONTROL_LABELS, language)


def memory_label(language="en"):
    """The memory word; a stale one adds its reason, labelled from the fixed code (@cairnmind_stale_reason)."""
    from .terminal_i18n import tr
    from .workspace import STALE_WORDS
    reason = _pick("stale_reason", STALE_WORDS, language, prefix=": ")
    plain = {value: label for value, label in MEMORY_LABELS.items() if value != "stale"}
    return ("#{?#{m:stale*,#{@cairnmind_memory}}," + _literal(tr(MEMORY_LABELS["stale"], language)) + reason + ","
            + _pick("memory", plain, language) + "}")


def left_width(language="en"):
    """Cells the left part of a border can take at its widest: three labels, the spaces around them and a gap."""
    from .terminal_i18n import tr
    from .workspace import STALE_WORDS
    widest = lambda labels: max(cell_len(_literal(tr(label, language))) for label in labels)  # noqa: E731
    stale = cell_len(_literal(tr(MEMORY_LABELS["stale"], language))) + 2 + widest(STALE_WORDS.values())
    return 6 + widest(ROLE_LABELS.values()) + widest(CONTROL_LABELS.values()) + max(widest(MEMORY_LABELS.values()), stale)
