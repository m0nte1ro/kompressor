"""Cheap content evidence for a source file: a digest of evenly spaced samples.

Size and mtime alone cannot detect an in-place edit that keeps both (for
example a tag editor that restores timestamps). The samples always include the
start and end of the file, where container headers and tags live.
"""
import os
from hashlib import sha256
from pathlib import Path

SCHEME = "sha256-16x1MiB-v1"
CHUNK = 1024 * 1024
SAMPLES = 16


def sample_digest(path: Path) -> str:
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        digest = sha256(str(size).encode())
        if size <= CHUNK * SAMPLES:
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
        else:
            for sample in range(SAMPLES):
                handle.seek((size - CHUNK) * sample // (SAMPLES - 1))
                digest.update(handle.read(CHUNK))
    return f"{SCHEME}:{digest.hexdigest()}"
