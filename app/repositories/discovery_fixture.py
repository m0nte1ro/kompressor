"""Fixture-only observation/probe source; never stat, walk, hash or probe media."""
from pathlib import Path

from app.models.inventory import FileObservation, ScanSnapshot
from app.models.probe import MediaProbeResult
from app.services.errors import NotFound


class FixtureObservationSource:
    def __init__(self):
        self.observations: dict[tuple[str, str], FileObservation] = {}

    def load_snapshot(self, snapshot: ScanSnapshot) -> None:
        # Model a fresh view, including removals. Partial/unavailable roots are
        # not sufficient evidence for guard checks of omitted files.
        self.observations = {key: value for key, value in self.observations.items() if key[0] != snapshot.root_id}
        self.observations.update({(item.root_id, item.relative_path): item.model_copy(deep=True)
                                  for item in snapshot.files})

    def observe(self, root_id: str, relative_path: str) -> FileObservation | None:
        item = self.observations.get((root_id, relative_path))
        return item.model_copy(deep=True) if item is not None else None

    def probe(self, root_id: str, relative_path: str) -> MediaProbeResult:
        item = self.observe(root_id, relative_path)
        if item is None or item.probe is None:
            raise NotFound("No fixture probe result for this file.")
        return item.probe.model_copy(deep=True)


def read_snapshot(path: Path) -> ScanSnapshot:
    return ScanSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
