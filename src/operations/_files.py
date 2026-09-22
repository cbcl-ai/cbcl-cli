"""Bounded operator inputs must be regular files, stable for the read receipt."""

from contextlib import contextmanager
import os
from pathlib import Path
import stat


def _identity(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


@contextmanager
def regular_reader(path: Path):
    # O_NONBLOCK prevents a substituted FIFO from hanging before the type check.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise ValueError("Operator input must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
            final = os.fstat(descriptor)
            named = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(named.st_mode)
                or _identity(initial) != _identity(final)
                or _identity(final) != _identity(named)
            ):
                raise ValueError(
                    "Operator input changed or was replaced during verification"
                )
    finally:
        os.close(descriptor)


def bounded_bytes(path: Path, maximum: int) -> bytes:
    with regular_reader(path) as stream:
        if os.fstat(stream.fileno()).st_size > maximum:
            raise ValueError("Operator input exceeds metadata budget")
        content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise ValueError("Operator input exceeds metadata budget")
        return content
