from pathlib import Path
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
