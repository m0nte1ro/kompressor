"""Read-only local discovery. No hashing, probing, media mutation or symlink traversal."""
import os
import re
import stat
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.models.inventory import FileObservation, InventoryState, ScanSnapshot
from app.models.media import MediaScope

SUPPORTED_EXTENSIONS = {'.mkv', '.mp4', '.m4v', '.avi', '.mov', '.ts', '.m2ts', '.mpg', '.mpeg', '.webm'}
EPISODE = re.compile(r'(?i)(?<![a-z0-9])S(\d{1,3})E(\d{1,4})(?!\d)')


def root_identity(scope: MediaScope, root: Path) -> str:
    return 'filesystem:' + str(uuid5(NAMESPACE_URL, f'{scope}:{root.absolute()}'))


def semantic_key(scope: MediaScope, relative: str) -> str | None:
    path = Path(relative)
    if scope == 'movie':
        return str(path.with_suffix(''))
    match = EPISODE.search(path.stem)
    if match is None:
        return None
    show = path.parts[0] if len(path.parts) > 1 else path.stem[:match.start()].strip(' .-_')
    return f'{show}:S{int(match[1])}E{int(match[2])}'


class FilesystemScanner:
    def scan(self, root: Path, scope: MediaScope, state: InventoryState) -> tuple[ScanSnapshot, list[str]]:
        root = root.absolute()
        root_id = root_identity(scope, root)
        sequence = state.scan_sequences.get(root_id, 0) + 1
        snapshot = ScanSnapshot(root_id=root_id, sequence=sequence, status='complete')
        errors: list[str] = []
        existing = [f for f in state.files.values() if f.root_id == root_id]
        def failure(error: OSError):
            errors.append(str(error))
            snapshot.status = 'partial'
        try:
            if not root.is_dir() or root.is_symlink():
                raise OSError(f'Root is unavailable or a symlink: {root}')
            # Explicitly test enumeration; os.walk otherwise reports a missing root ambiguously.
            with os.scandir(root):
                pass
        except OSError as error:
            snapshot.status = 'unavailable'
            return snapshot, [str(error)]
        for directory, folders, filenames in os.walk(root, followlinks=False, onerror=failure):
            folders[:] = sorted(name for name in folders if not Path(directory, name).is_symlink())
            for name in sorted(filenames):
                path = Path(directory, name)
                if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                try:
                    info = path.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    if not os.access(path, os.R_OK):
                        raise OSError(f'File is not readable: {path}')
                    relative = path.relative_to(root).as_posix()
                    same_path = next((f for f in existing if f.relative_path == relative), None)
                    physical = [f for f in existing if f.observation.filesystem_id == str(info.st_dev)
                                and f.observation.inode == info.st_ino
                                and f.observation.size == info.st_size and f.observation.mtime_ns == info.st_mtime_ns]
                    previous = same_path or (physical[0] if len(physical) == 1 else None)
                    key = semantic_key(scope, relative)
                    if previous is None and key is None:
                        errors.append(f'Unrecognized episode name (expected SxxExx): {relative}')
                        continue
                    media_id = previous.media_id if previous else 'filesystem:' + str(uuid5(NAMESPACE_URL, f'{root_id}:{key}'))
                    snapshot.files.append(FileObservation(
                        root_id=root_id, relative_path=relative, media_id=media_id, scope=scope,
                        filesystem_id=str(info.st_dev), inode=info.st_ino,
                        size=info.st_size, mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns,
                        hardlinks=info.st_nlink,
                    ))
                except OSError as error:
                    failure(error)
        return snapshot, errors
