# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .
 

from __future__ import annotations

import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextmanager
def exclusive_file_lock(lock_path: Path) -> Generator[None, None, None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lf:
        if sys.platform == "win32":
            msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lf, fcntl.LOCK_UN)
