from pydantic import BaseModel, ConfigDict, Field


class LibraryPaths(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    movies_path: str = Field(default="/media/movies", min_length=1, max_length=500)
    shows_path: str = Field(default="/media/shows", min_length=1, max_length=500)
