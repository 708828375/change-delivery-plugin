from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import tempfile
from typing import Iterator

from filelock import FileLock, Timeout

from change_delivery.errors import BlockedError


def project_lock_path(
    repo_path: Path,
    *,
    lock_root: Path | None = None,
) -> Path:
    canonical = str(Path(repo_path).resolve(strict=True)).casefold()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    root = lock_root or (
        Path(tempfile.gettempdir()) / "change-delivery-project-locks"
    )
    return root / f"{digest}.lock"


@contextmanager
def project_read_lock(
    repo_path: Path,
    *,
    lock_root: Path | None = None,
) -> Iterator[None]:
    path = project_lock_path(repo_path, lock_root=lock_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(path, timeout=0)
    try:
        with lock:
            yield
    except Timeout as error:
        raise BlockedError("project delivery lock is held") from error
