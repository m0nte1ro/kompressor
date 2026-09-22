"""Compare supplied evidence. No hashing or filesystem reads happen here."""
from app.models.inventory import FileObservation


def full_match(left: FileObservation, right: FileObservation) -> bool | None:
    a, b = left.fingerprints, right.fingerprints
    if a.full is None or b.full is None or a.full_scheme != b.full_scheme:
        return None
    return left.size == right.size and a.full == b.full


def sample_match(left: FileObservation, right: FileObservation) -> bool | None:
    a, b = left.fingerprints, right.fingerprints
    if a.sample is None or b.sample is None or a.sample_scheme != b.sample_scheme:
        return None
    return left.size == right.size and a.sample == b.sample


def unchanged(left: FileObservation, right: FileObservation) -> bool:
    full = full_match(left, right)
    if full is not None:
        return full
    if sample_match(left, right) is False:
        return False
    # Metadata is the cheap tier, never a cryptographic guarantee. Unknown
    # filesystem identity cannot establish an unchanged file on its own.
    return (left.physical_key() is not None and left.physical_key() == right.physical_key()
            and left.size == right.size and left.mtime_ns == right.mtime_ns)
