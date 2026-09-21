from pathlib import Path

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

    media_backend: str = "seed"

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
