"""Give freed memory back to the operating system (#410).

A pass that builds a large structure in a worker thread (indexing a project's graph, the inspector's
whole-graph read) frees it again, but glibc keeps what a thread's own arena held: one arena per thread
that ever allocated much, so memory grew by tens of megabytes per pass until every thread had its peak.
The service allocates under the GIL, so a second arena buys nothing; `limit_arenas` keeps it at two and
`release_freed` returns the free pages after a pass. Only where glibc is: on any other system both do nothing
and say so with False.
"""
from __future__ import annotations

import ctypes
import functools
import sys

M_ARENA_MAX = -8  # <malloc.h>


@functools.cache
def _libc():
    if not sys.platform.startswith("linux"):
        return None
    try:
        return ctypes.CDLL(None)
    except OSError:
        return None


def limit_arenas(count: int = 2) -> bool:
    """At most `count` malloc arenas from now on. Call it before the threads that allocate are started."""
    libc = _libc()
    return bool(libc is not None and hasattr(libc, "mallopt") and libc.mallopt(M_ARENA_MAX, count) == 1)


def release_freed() -> bool:
    """Hand the free pages of every arena back to the system; True when the library did it."""
    libc = _libc()
    return bool(libc is not None and hasattr(libc, "malloc_trim") and libc.malloc_trim(0) in (0, 1))
