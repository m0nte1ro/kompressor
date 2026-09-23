from pathlib import Path
from typing import Literal
from pydantic import Field

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

    development_mode: bool = True

    media_backend: Literal["seed", "filesystem"] = "seed"
    movies_root: Path | None = None
    shows_root: Path | None = None
    ffprobe_binary: str = "ffprobe"
    ffprobe_timeout: float = Field(default=30, gt=0, le=600)

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
