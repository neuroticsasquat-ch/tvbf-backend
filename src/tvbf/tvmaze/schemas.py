from datetime import date, time
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _empty_to_none(v: Any) -> Any:
    if v == "":
        return None
    return v


# TV Maze returns empty strings rather than null for unknown date/time values.
OptionalDate = Annotated[date | None, BeforeValidator(_empty_to_none)]
OptionalTime = Annotated[time | None, BeforeValidator(_empty_to_none)]


class TVMazeNetwork(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    country: dict | None = None

    @property
    def country_code(self) -> str | None:
        return (self.country or {}).get("code")

    @property
    def country_name(self) -> str | None:
        return (self.country or {}).get("name")

    @property
    def timezone(self) -> str | None:
        return (self.country or {}).get("timezone")


class TVMazeImage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    medium: str | None = None
    original: str | None = None


class TVMazeEpisode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    season: int
    number: int | None = None
    name: str | None = None
    airdate: OptionalDate = None
    airtime: OptionalTime = None
    runtime: int | None = None
    summary: str | None = None
    image: TVMazeImage | None = None


class TVMazeSeason(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    number: int
    name: str | None = None
    episodeOrder: int | None = None
    premiereDate: OptionalDate = None
    endDate: OptionalDate = None
    network: TVMazeNetwork | None = None
    webChannel: TVMazeNetwork | None = None
    image: TVMazeImage | None = None
    summary: str | None = None


class TVMazeExternals(BaseModel):
    model_config = ConfigDict(extra="ignore")

    imdb: str | None = None
    tvdb: int | None = None
    tvrage: int | None = None


class TVMazeEmbedded(BaseModel):
    model_config = ConfigDict(extra="ignore")

    episodes: list[TVMazeEpisode] = Field(default_factory=list)
    seasons: list[TVMazeSeason] = Field(default_factory=list)


class TVMazeShow(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    name: str
    type: str | None = None
    language: str | None = None
    status: str | None = None
    runtime: int | None = None
    premiered: OptionalDate = None
    ended: OptionalDate = None
    officialSite: str | None = None
    summary: str | None = None
    image: TVMazeImage | None = None
    externals: TVMazeExternals | None = None
    network: TVMazeNetwork | None = None
    webChannel: TVMazeNetwork | None = None
    genres: list[str] = Field(default_factory=list)
    updated: int
    embedded: TVMazeEmbedded = Field(default_factory=TVMazeEmbedded, alias="_embedded")
