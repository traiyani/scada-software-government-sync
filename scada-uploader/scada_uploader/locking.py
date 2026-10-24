"""One uploader process at a time. Task Scheduler should also be set to
not start a new instance, and this lock covers a second manual launch.
"""

from __future__ import annotations

import os
from pathlib import Path


def acquire(path: Path):
    """Return an open file handle holding the lock, or None if another process has it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == "":
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle
