"""Application scan -> probe -> reconcile use case, with serialized read-only scans."""
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread
from copy import deepcopy
from uuid import uuid4
from time import monotonic

from app.models.preferences import LibraryPaths
from app.models.inventory import ScanSnapshot
from app.services.errors import Conflict, InvalidOperation
from app.services.ffprobe import TechnicalProbe
from app.services.filesystem_scanner import FilesystemScanner, root_identity
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
        self.report_lock = Lock()
        self.stop = Event()
        self.thread: Thread | None = None
        self.last_report: dict = {'state': 'idle', 'roots': [], 'generation': 0}

    def status(self) -> dict:
        with self.report_lock:
            return {'backend': 'filesystem', **deepcopy(self.last_report)}

    def _publish(self, **changes):
        with self.report_lock:
            self.last_report = {**self.last_report, **deepcopy(changes),
                                'generation': self.last_report['generation'] + 1}

    def _prepare(self):
        if not self.lock.acquire(blocking=False):
            raise Conflict('A library scan is already running.')
        try:
            roots = self.roots()  # Capture saved roots once, at the operation boundary.
            if not roots:
                raise InvalidOperation('Configure at least one library root before scanning.')
            self.stop.clear()
            self._publish(state='running', scan_id=str(uuid4()), roots=[], error=None, phase='discovering', discovered_so_far=0, discovering_root=None)
            return roots
        except Exception:
            self.lock.release()
            raise

    def start(self) -> dict:
        roots = self._prepare()
        accepted = self.status()
        self.thread = Thread(target=self._background, args=(roots,), name='library-discovery', daemon=True)
        self.thread.start()
        return accepted

    def _background(self, roots):
        try:
            self._run(roots)
        except Exception:
            # _run records the failure for polling clients. No unobserved thread exception.
            pass

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join()

    def scan(self) -> dict:
        """Synchronous service boundary retained for fixtures/CLI; HTTP uses start()."""
        return self._run(self._prepare())

    def _run(self, roots) -> dict:
        reports = []
        pending = []
        try:
            # Publish each root's stat inventory before slow probing. Reconciliation
            # remains atomic per complete/partial root and never holds a lock over IO.
            for scope, root in roots.items():
                if self.stop.is_set():
                    break
                media_scope = 'movie' if scope == 'movie' else 'show'
                state = self.reconciliation.repository.root_state(root_identity(media_scope, root))
                sequence = state.scan_sequences.get(root_identity(media_scope, root), 0)
                discovered = 0
                def publish_new_batch(items):
                    nonlocal sequence, discovered
                    sequence += 1
                    discovered += len(items)
                    self.reconciliation.reconcile(ScanSnapshot(root_id=items[0].root_id, sequence=sequence,
                                                              status='partial', files=items))
                    self._publish(roots=reports, discovering_root=str(root), discovered_so_far=discovered)
                snapshot, errors = self.scanner.scan(root, media_scope, state,
                    on_batch=publish_new_batch if not state.files else None, cancelled=self.stop.is_set)
                snapshot.sequence = sequence + 1
                result = self.reconciliation.reconcile(snapshot)
                report = {'root': str(root), 'root_id': snapshot.root_id, 'status': snapshot.status,
                          'discovered': len(snapshot.files), 'probed': 0, 'processed': 0,
                          'errors': errors, 'reconciliation': result.model_dump()}
                reports.append(report)
                records = self.reconciliation.repository.files(root_id=snapshot.root_id, present=True)
                by_path = {f.relative_path: f for f in records}
                pending.append((root, snapshot, by_path, report))
                self._publish(roots=reports, discovered_so_far=0)
            self._publish(phase='probing')
            last_publish = monotonic()
            for root, snapshot, by_path, report in pending:
                for item in snapshot.files:
                    if self.stop.is_set():
                        break
                    record = by_path.get(item.relative_path)
                    if record is None or not unchanged(record.observation, item):
                        continue  # Unresolved identity or excluded output artifact.
                    report['processed'] += 1
                    if record.observation.probe is not None:
                        continue
                    try:
                        facts = self.probe.inspect(root / item.relative_path)
                        after = (root / item.relative_path).stat(follow_symlinks=False)
                        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                                int(item.filesystem_id or -1), item.inode, item.size, item.mtime_ns):
                            raise InvalidOperation('File changed during probing; retry the scan.')
                        observed = record.observation.model_copy(update={'probe': facts, 'hardlinks': after.st_nlink})
                        if self.reconciliation.repository.attach_probe(record.file_id, record.revision_id, observed):
                            report['probed'] += 1
                    except (InvalidOperation, OSError) as error:
                        report['errors'].append(f'{item.relative_path}: {error}')
                    self._publish(roots=reports, discovered_so_far=0)
            self._publish(state='cancelled' if self.stop.is_set() else 'completed', phase='idle', roots=reports)
            return self.status()
        except Exception as error:
            self._publish(state='failed', error=str(error), roots=reports)
            raise
        finally:
            self.lock.release()
