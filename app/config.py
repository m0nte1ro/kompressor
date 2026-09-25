from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import Field, field_validator

from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)


PROJECT_ROOT = (
    Path(__file__)
    .resolve()
    .parent
    .parent
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KOMPRESSOR_",
        env_file=".env",
        extra="ignore",
    )

    app_name: str = "Kompressor"

    development_mode: bool = Field(default=True, deprecated="Unused compatibility flag; select media_backend instead.")

    media_backend: Literal["seed", "filesystem"] = "seed"
    movies_root: Path | None = None
    shows_root: Path | None = None
    ffprobe_binary: str = Field(default="ffprobe", min_length=1)
    ffprobe_timeout: float = Field(default=30, gt=0, le=600)
    ffmpeg_binary: str = Field(default="ffmpeg", min_length=1)
    workspace_root: Path = Path("/mnt/kompressor")
    timezone: str = "UTC"


    @field_validator("workspace_root")
    @classmethod
    def absolute_workspace(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Workspace root must be an absolute path.")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Timezone must be a valid IANA timezone.") from error
        return value


    database_path: Path = PROJECT_ROOT / "data" / "kompressor.sqlite3"

    seed_media_path: Path = (
        PROJECT_ROOT
        / "fixtures"
        / "media.json"
    )

    seed_presets_path: Path = (
        PROJECT_ROOT
        / "fixtures"
        / "presets.json"
    )


settings = Settings()
