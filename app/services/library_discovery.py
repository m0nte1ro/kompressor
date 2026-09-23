"""Application scan -> probe -> reconcile use case, with serialized read-only scans."""
from collections.abc import Callable
from pathlib import Path
from threading import Lock

from app.models.preferences import LibraryPaths
from app.services.errors import Conflict, InvalidOperation
from app.services.ffprobe import TechnicalProbe
from app.services.filesystem_scanner import FilesystemScanner
from app.services.fingerprints import unchanged
from app.services.reconciliation import ReconciliationService


def configured_roots(paths: LibraryPaths, database_path: Path) -> dict[str, Path]:
    roots = {scope: Path(value).absolute() for scope, value in
             (("movie", paths.movies_path), ("show", paths.shows_path)) if value}
    for root in roots.values():
        if database_path.resolve().is_relative_to(root.resolve()):
            raise InvalidOperation("Application database must be outside media roots.")
    if len(roots) == 2:
        movie, show = roots["movie"].resolve(), roots["show"].resolve()
        if movie.is_relative_to(show) or show.is_relative_to(movie):
            raise InvalidOperation("Movie and show roots must not overlap.")
    return roots


class LibraryDiscoveryService:
    def __init__(self, scanner: FilesystemScanner, probe: TechnicalProbe,
                 reconciliation: ReconciliationService, roots: Callable[[], dict[str, Path]], database_path: Path):
        self.scanner = scanner
        self.probe = probe
        self.reconciliation = reconciliation
        self.roots = roots
        self.database_path = database_path
        self.lock = Lock()
        self.last_report: dict = {'state': 'idle', 'roots': []}

    def status(self) -> dict:
        return {'backend': 'filesystem', **self.last_report}

    def scan(self) -> dict:
        if not self.lock.acquire(blocking=False):
            raise Conflict('A library scan is already running.')
        self.last_report = {'state': 'running', 'roots': []}
        try:
            roots = self.roots()
            if not roots:
                raise InvalidOperation('Configure at least one library root before scanning.')
            reports = []
            for scope, root in roots.items():
                state = self.reconciliation.repository.load()
                snapshot, errors = self.scanner.scan(root, 'movie' if scope == 'movie' else 'show', state)
                probed = 0
                for index, item in enumerate(snapshot.files):
                    previous = next((f for f in state.files.values() if f.root_id == item.root_id
                                     and f.media_id == item.media_id and unchanged(f.observation, item)
                                     and f.observation.probe is not None), None)
                    if previous:
                        snapshot.files[index] = item.model_copy(update={'probe': previous.observation.probe})
                        continue
                    try:
                        facts = self.probe.inspect(root / item.relative_path)
                        # Discard facts if the input changed while ffprobe was reading.
                        after = (root / item.relative_path).stat(follow_symlinks=False)
                        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                                int(item.filesystem_id or -1), item.inode, item.size, item.mtime_ns):
                            raise InvalidOperation('File changed during probing; retry the scan.')
                        snapshot.files[index] = item.model_copy(update={'probe': facts, 'hardlinks': after.st_nlink})
                        probed += 1
                    except (InvalidOperation, OSError) as error:
                        errors.append(f'{item.relative_path}: {error}')
                        # Keep the discovered file, with unknown metadata. A failed
                        # probe is not evidence that the source is missing.
                result = self.reconciliation.reconcile(snapshot)
                reports.append({'root': str(root), 'root_id': snapshot.root_id,
                                'status': snapshot.status, 'discovered': len(snapshot.files),
                                'probed': probed, 'errors': errors, 'reconciliation': result.model_dump()})
            self.last_report = {'state': 'completed', 'roots': reports}
            return self.status()
        except Exception:
            self.last_report = {**self.last_report, 'state': 'failed'}
            raise
        finally:
            self.lock.release()
