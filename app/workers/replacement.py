"""In-place source replacement after a fully validated encode.

The original is never overwritten or deleted until the replacement has been
verified at the source path:

1. copy the validated workspace output next to the source (same directory, so
   the swap is a rename on one filesystem) and verify the copy by SHA-256;
2. rename the original to a hidden backup, then the copy into the source path;
3. validate the file now at the source path;
4. only then delete the backup.

Every step is journalled on the job first. Any failure, and any interruption
found at worker startup, is resolved by identity (device/inode) checks: a file
is only unlinked when it is provably Kompressor's own copy or its own backup of
a source that is verifiably back in place. Anything else is left untouched and
reported for manual review.
"""
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Literal
import os
import shutil
import stat

from app.models.queue import ReplacementJournal

CHUNK_SIZE = 8 * 1024 * 1024
# Keep headroom on the media filesystem beyond the copy itself.
SPACE_MARGIN = 256 * 1024 * 1024

Outcome = Literal["untouched", "restored", "replaced", "manual"]


class ReplacementError(RuntimeError):
    pass


class ReplacementAborted(ReplacementError):
    """Stopped before the original was touched."""


def replacement_reasons(path: Path) -> list[str]:
    """Static preconditions checked when queueing, claiming and replacing."""
    reasons = []
    if path.suffix.lower() != ".mkv":
        reasons.append("Source replacement requires an MKV source (the output is Matroska); "
                       "choose Keep original for this file.")
    try:
        if os.statvfs(path.parent).f_flag & getattr(os, "ST_RDONLY", 1):
            reasons.append("The source directory is on a read-only mount; replacement is impossible.")
        elif not os.access(path.parent, os.W_OK | os.X_OK):
            reasons.append("The source directory is not writable by Kompressor; replacement is impossible.")
    except OSError as error:
        reasons.append(f"The source directory cannot be checked: {error}")
    return reasons


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


def _digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)



class SourceReplacer:
    def plan(self, job_id: str, target: Path, output_size: int) -> ReplacementJournal:
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ReplacementError("The source is not a regular file.")
        # Job IDs are validated workspace names; keep staging names short and job-unique.
        return ReplacementJournal(
            target=str(target), incoming=str(target.parent / f".{job_id}.kompressor-incoming"),
            backup=str(target.parent / f".{job_id}.kompressor-backup"),
            source_device=info.st_dev, source_inode=info.st_ino, output_size=output_size)

    def replace(self, journal: ReplacementJournal, output: Path, *,
                before_swap: Callable[[], None], verify: Callable[[Path], list[str]],
                save: Callable[[ReplacementJournal], None],
                should_abort: Callable[[], bool] = lambda: False) -> list[str]:
        """Swap the verified output into the source path. Returns completion notes.

        Raises ReplacementError when the source was not replaced; its message says
        whether the original is untouched, restored or needs manual review.
        """
        target, incoming, backup = Path(journal.target), Path(journal.incoming), Path(journal.backup)
        try:
            notes = self._replace(journal, target, incoming, backup, output,
                                  before_swap, verify, save, should_abort)
        except BaseException as error:
            outcome, detail = self.resolve(journal, finalize=False)
            if outcome == "replaced":
                return [f"Replacement finished despite a late error: {error}"]
            raise ReplacementError(f"{error} {detail}") from error
        return notes

    def _replace(self, journal, target: Path, incoming: Path, backup: Path, output: Path,
                 before_swap, verify, save, should_abort) -> list[str]:
        reasons = replacement_reasons(target)
        if reasons:
            raise ReplacementAborted(" ".join(reasons))
        source = target.lstat()
        if (source.st_dev, source.st_ino) != (journal.source_device, journal.source_inode):
            raise ReplacementAborted("The source file changed identity before replacement.")
        if os.path.lexists(incoming) or os.path.lexists(backup):
            raise ReplacementAborted("A replacement staging file already exists next to the source.")
        size = output.stat().st_size
        if size != journal.output_size:
            raise ReplacementAborted("The validated output changed size before replacement.")
        free = shutil.disk_usage(target.parent).free
        if free < size + SPACE_MARGIN:
            raise ReplacementAborted(
                f"Not enough free space next to the source for the verified copy "
                f"({size + SPACE_MARGIN} bytes needed, {free} free).")

        # 1. Copy into the source directory and prove the copy on disk.
        save(journal)
        expected = sha256()
        descriptor = os.open(incoming, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as destination, output.open("rb") as origin:
            while chunk := origin.read(CHUNK_SIZE):
                if should_abort():
                    raise ReplacementAborted("Worker stopping; replacement aborted before the swap.")
                expected.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        # Preserve the original mode. Do not attempt to preserve owner/group:
        # on unprivileged LXC bind mounts the worker can create/rename/delete via
        # ACLs, but chown to the host file owner is intentionally unavailable.
        try:
            os.chmod(incoming, stat.S_IMODE(source.st_mode))
        except OSError as error:
            raise ReplacementAborted(f"Could not give the replacement the source's mode: {error}") from error
        copied = incoming.lstat()
        journal.incoming_device, journal.incoming_inode = copied.st_dev, copied.st_ino
        save(journal)
        if copied.st_size != size or _digest(incoming) != expected.hexdigest():
            raise ReplacementAborted("The copy next to the source does not match the validated output.")
        journal.phase = "copied"
        save(journal)

        # 2. Fresh source checks immediately before the swap; this is the last abort point.
        if should_abort():
            raise ReplacementAborted("Worker stopping; replacement aborted before the swap.")
        before_swap()
        if _identity(target) != (journal.source_device, journal.source_inode):
            raise ReplacementAborted("The source file changed identity before the swap.")
        journal.phase = "swapping"
        save(journal)
        os.rename(target, backup)
        os.rename(incoming, target)
        _fsync_directory(target.parent)
        journal.phase = "swapped"
        save(journal)

        # 3. Validate what is actually at the source path now.
        errors = []
        final = target.lstat()
        if (final.st_dev, final.st_ino) != (journal.incoming_device, journal.incoming_inode):
            errors.append("The file at the source path is not the verified replacement.")
        elif final.st_size != size:
            errors.append("The replaced file size does not match the validated output.")
        else:
            errors.extend(verify(target))
        if errors:
            raise ReplacementError("Replaced file failed final validation: " + "; ".join(errors))
        journal.phase = "verified"
        save(journal)

        # 4. The replacement is verified; the backup can go.
        notes = []
        if _identity(backup) == (journal.source_device, journal.source_inode):
            try:
                backup.unlink()
                _fsync_directory(target.parent)
            except OSError as error:
                notes.append(f"Source replaced, but the original backup could not be removed: {backup} ({error}).")
        else:
            notes.append(f"Source replaced; the expected original backup was not found at {backup}.")
        return notes

    def resolve(self, journal: ReplacementJournal, *, finalize: bool) -> tuple[Outcome, str]:
        """Bring an interrupted/failed replacement to a safe, known state.

        finalize=True (startup recovery) completes a swap that had already been
        verified; otherwise any swapped file is rolled back to the original.
        """
        target, incoming, backup = Path(journal.target), Path(journal.incoming), Path(journal.backup)
        original = (journal.source_device, journal.source_inode)
        ours = ((journal.incoming_device, journal.incoming_inode)
                if journal.incoming_inode is not None else None)
        try:
            at_target, at_backup, at_incoming = _identity(target), _identity(backup), _identity(incoming)
            if at_backup is None:
                if at_target == original:
                    # Never swapped. The incoming name is unique to this job; only
                    # remove it when it is our copy or the copy was still being written.
                    if at_incoming is not None and (ours is None or at_incoming == ours):
                        incoming.unlink()
                    return "untouched", "The original was not modified."
                if ours is not None and at_target == ours:
                    return "replaced", "The verified replacement is in place."
                return "manual", self._manual(journal, "the source path does not hold the original or the replacement")
            if at_backup != original:
                return "manual", self._manual(journal, "the backup is not the recorded original")
            if at_target is None:
                os.rename(backup, target)
                # Interrupted between the two renames: the verified copy never moved in.
                if ours is not None and at_incoming == ours:
                    incoming.unlink()
                _fsync_directory(target.parent)
                return "restored", "The original was restored."
            if ours is not None and at_target == ours:
                if finalize and journal.phase == "verified":
                    backup.unlink()
                    _fsync_directory(target.parent)
                    return "replaced", "The verified replacement is in place; the backup was removed."
                target.unlink()
                os.rename(backup, target)
                _fsync_directory(target.parent)
                return "restored", "The replacement was rolled back and the original restored."
            return "manual", self._manual(journal, "an unknown file is at the source path")
        except OSError as error:
            return "manual", self._manual(journal, f"recovery failed ({error})")

    @staticmethod
    def _manual(journal: ReplacementJournal, why: str) -> str:
        return (f"MANUAL CHECK REQUIRED: {why}. Nothing further was changed. "
                f"Source path: {journal.target}; original backup: {journal.backup}; "
                f"replacement copy: {journal.incoming}.")
