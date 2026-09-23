"""Fresh, read-only source stat observations for SourceGuard."""
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
            for part in rel.parts:
                path = path / part
                info = path.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    return None
            if not stat.S_ISREG(info.st_mode):
                return None
            return path
        except OSError:
            return None

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
        return record.observation.model_copy(update={
            "filesystem_id": str(info.st_dev), "inode": info.st_ino,
            "size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns, "hardlinks": info.st_nlink,
        })
