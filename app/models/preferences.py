from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, field_validator


class LibraryPaths(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    movies_path: str = Field(default="", min_length=0, max_length=500)
    shows_path: str = Field(default="", min_length=0, max_length=500)

    @field_validator("movies_path", "shows_path")
    @classmethod
    def absolute_or_empty(cls, value: str) -> str:
        if value and not Path(value).is_absolute():
            raise ValueError("Library roots must be absolute paths, or empty to disable the root.")
        return value


def _valid_clock(value: str) -> str:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("Time must use HH:MM.")
    try:
        hour, minute = (int(part) for part in parts)
    except ValueError as error:
        raise ValueError("Time must use HH:MM.") from error
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Time must use HH:MM.")
    return f"{hour:02d}:{minute:02d}"


class WorkerLaneSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paused: bool = False
    quiet_hours_enabled: bool = False
    quiet_start: str = "18:00"
    quiet_end: str = "01:00"
    quiet_cutoff_percent: float = Field(default=50.0, ge=0, le=100)

    @field_validator("quiet_start", "quiet_end")
    @classmethod
    def valid_clock(cls, value: str) -> str:
        return _valid_clock(value)


class WorkerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    timezone: str = Field(default="UTC", min_length=1, max_length=100)
    cpu: WorkerLaneSettings = Field(default_factory=WorkerLaneSettings)
    qsv: WorkerLaneSettings = Field(default_factory=WorkerLaneSettings)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Timezone must be a valid IANA timezone, for example Europe/Lisbon.") from error
        return value
