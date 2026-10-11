"""Fresh, read-only source stat observations for SourceGuard."""
import os
import stat
from pathlib import Path, PurePosixPath
from collections.abc import Callable

from app.models.inventory import FileObservation, SourceReference
from app.repositories.inventory import InventoryRepository
from app.services.filesystem_scanner import root_identity


class FilesystemObservationSource:
    def __init__(self, inventory: InventoryRepository, roots: Callable[[], dict[str, Path]]):
        self.inventory = inventory
        self.roots = roots

    def _root(self, root_id: str) -> Path | None:
        for scope, root in self.roots().items():
            media_scope = "movie" if scope == "movie" else "show"
            if root_identity(media_scope, root) == root_id:
                return root
        return None

    def _safe_path(self, root: Path, relative_path: str) -> Path | None:
        rel = PurePosixPath(relative_path)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or root.is_symlink():
            return None
        path = root
        try:
            if not path.is_dir():
                return None
            final_mode: int | None = None
            for part in rel.parts:
                path = path / part
                info = path.stat(follow_symlinks=False)
                final_mode = info.st_mode
                if stat.S_ISLNK(final_mode):
                    return None
            if final_mode is None or not stat.S_ISREG(final_mode):
                return None
            return path
        except OSError:
            return None

    def root_available(self, root_id: str) -> bool | None:
        """Whether a configured root can be listed and is not an unmounted mount point.

        An unmounted mount point is an empty directory. As in the scanner, an empty
        root is unavailable only while the inventory still lists files on it; once
        the scans have recorded them missing it is simply empty, and jobs on it are
        judged normally. None when no configured root has this ID: removing a root
        is a setting, not an outage.
        """
        root = self._root(root_id)
        if root is None:
            return None
        try:
            with os.scandir(root) as entries:
                if next(entries, None) is not None:
                    return True
        except OSError:
            return False
        return not self.inventory.has_present_files(root_id)

    def reference_for(self, file_id: str, revision_id: str) -> SourceReference | None:
        record = self.inventory.get_file(file_id)
        if record is None or record.revision_id != revision_id or record.presence != "present":
            return None
        observation = record.observation
        return SourceReference(file_id=file_id, revision_id=revision_id, captured=True,
            root_id=record.root_id, relative_path=record.relative_path,
            filesystem_id=observation.filesystem_id, inode=observation.inode,
            generation=observation.generation, size=observation.size,
            mtime_ns=observation.mtime_ns, ctime_ns=observation.ctime_ns,
            hardlinks=observation.hardlinks)

    def path_for(self, reference: SourceReference) -> Path | None:
        record = self.inventory.get_file(reference.file_id)
        if record is None or record.revision_id != reference.revision_id or record.presence != "present":
            return None
        if (reference.root_id is not None and reference.root_id != record.root_id
                or reference.relative_path is not None and reference.relative_path != record.relative_path):
            return None
        root = self._root(record.root_id)
        return self._safe_path(root, record.relative_path) if root else None

    def observe(self, root_id: str, relative_path: str) -> FileObservation | None:
        """A fresh stat of a present library file, and nothing it did not see.

        Device, inode, size, mtime, ctime and hardlink count are observed. The file
        is not read, so it has no fingerprints or probe, and Linux reports no inode
        generation (scans record none either). Root, path, media ID and scope only
        name the inventory entry that was looked at.
        """
        root = self._root(root_id)
        if root is None:
            return None
        rows = self.inventory.files(root_id=root_id, path=relative_path, present=True)
        if not rows:
            return None
        record = rows[0]
        path = self._safe_path(root, relative_path)
        if path is None:
            return None
        try:
            info = path.stat(follow_symlinks=False)
        except OSError:
            return None
        return FileObservation(
            root_id=record.root_id, relative_path=record.relative_path,
            media_id=record.media_id, scope=record.scope,
            filesystem_id=str(info.st_dev), inode=info.st_ino,
            size=info.st_size, mtime_ns=info.st_mtime_ns,
            ctime_ns=info.st_ctime_ns, hardlinks=info.st_nlink)
