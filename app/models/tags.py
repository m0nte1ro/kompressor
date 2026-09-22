from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


TagName = Literal["Preserve A/V", "Preserve Video", "Preserve Audio", "Quality CPU", "Quality Floor"]
TAG_NAMES = ["Preserve A/V", "Preserve Video", "Preserve Audio", "Quality CPU", "Quality Floor"]


class QualityFloor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minimum_video_bitrate: int = Field(gt=0, le=200_000_000)
    minimum_height: Literal[480, 720, 1080, 2160]


class TagTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["movie", "show", "season", "episode"]
    id: str = Field(min_length=1, max_length=200)
    season: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def season_target(self):
        if (self.kind == "season") != (self.season is not None):
            raise ValueError("Only season targets must include a season number.")
        return self

    def key(self) -> str:
        # Namespaced to the seed inventory; future scanners must supply stable IDs.
        return "seed:" + self.model_dump_json()


class TagAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tags: list[TagName] = Field(default_factory=list, max_length=5)
    quality_floor: QualityFloor | None = None

    @model_validator(mode="after")
    def floor_requires_values(self):
        self.tags = list(dict.fromkeys(self.tags))
        if getattr(self, "operation", None) == "remove":
            if self.quality_floor is not None:
                raise ValueError("Removing a tag does not accept floor settings.")
            return self
        if ("Quality Floor" in self.tags) != (self.quality_floor is not None):
            raise ValueError("Quality Floor requires explicit bitrate and resolution limits.")
        return self


class TagUpdate(TagAssignment):
    targets: list[TagTarget] = Field(min_length=1, max_length=500)
    operation: Literal["replace", "add", "remove"] = "replace"

    @model_validator(mode="after")
    def bulk_replace(self):
        if self.operation == "replace" and len(self.targets) != 1:
            raise ValueError("Bulk changes must add or remove tags, preserving unrelated tags.")
        return self
