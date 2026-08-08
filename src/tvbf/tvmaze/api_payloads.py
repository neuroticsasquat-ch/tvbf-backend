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


class TVMazeAka(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    country: dict | None = None
    language: str | None = None

    @property
    def country_code(self) -> str | None:
        return (self.country or {}).get("code")

    @property
    def country_name(self) -> str | None:
        return (self.country or {}).get("name")


class TVMazeImage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    medium: str | None = None
    original: str | None = None


class TVMazeRating(BaseModel):
    model_config = ConfigDict(extra="ignore")

    average: float | None = None


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
    rating: TVMazeRating | None = None

    @property
    def rating_average(self) -> float | None:
        return self.rating.average if self.rating else None


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


class TVMazePerson(BaseModel):
    """A person as embedded in a show's cast/crew.

    The embedded object is complete — same shape as `/people/{id}` — so the
    show axis populates `tvmaze.person` fully as a side effect of pass A.
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    country: dict | None = None
    birthday: OptionalDate = None
    deathday: OptionalDate = None
    gender: str | None = None
    image: TVMazeImage | None = None
    # Required, like TVMazeShow.updated. Defaulting it to 0 would let a payload
    # missing `updated` silently reset a person's watermark on re-upsert, which
    # is the person delta's ordering key.
    updated: int

    @property
    def country_code(self) -> str | None:
        return (self.country or {}).get("code")

    @property
    def country_name(self) -> str | None:
        return (self.country or {}).get("name")

    @property
    def timezone(self) -> str | None:
        return (self.country or {}).get("timezone")


class TVMazeCharacter(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    image: TVMazeImage | None = None


class TVMazeCastEntry(BaseModel):
    # `self` can't be a field name, so both flags are aliased for symmetry.
    # `character` is never null upstream (286/286 sampled), including pure
    # "self" appearances, so it is modelled non-nullable.
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    person: TVMazePerson
    character: TVMazeCharacter
    is_self: bool = Field(False, alias="self")
    is_voice: bool = Field(False, alias="voice")


class TVMazeCrewEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    person: TVMazePerson


class TVMazeEpisodeCrewEntry(BaseModel):
    """One episode-crew entry from a season episode list's `guestcrew` embed.

    Two differences from show-level crew, both easy to miss. The role field is
    camelCase `guestCrewType`, not `type` — reading it as `type` yields a
    validation error on every episode that has crew. And there is no
    `character`: an episode's director plays nobody, so there is nothing to
    intern on that side.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    type: str = Field(alias="guestCrewType")
    person: TVMazePerson


class TVMazeEpisodeEmbedded(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # `guestcast` is shaped exactly like show-level cast — person, character and
    # the two boolean flags — so it reuses TVMazeCastEntry rather than a
    # near-identical twin.
    guestcast: list[TVMazeCastEntry] = Field(default_factory=list)
    guestcrew: list[TVMazeEpisodeCrewEntry] = Field(default_factory=list)


class TVMazeSeasonEpisode(TVMazeEpisode):
    """An episode from `/seasons/{id}/episodes` carrying both credit embeds.

    `_embedded` is the same alias trap as `self`: spell it wrong and every
    episode parses cleanly with zero credits, which is indistinguishable from
    an episode that genuinely has none.
    """

    embedded: TVMazeEpisodeEmbedded = Field(
        default_factory=TVMazeEpisodeEmbedded, alias="_embedded"
    )


class TVMazeExternals(BaseModel):
    # Alias only, deliberately without `populate_by_name`: `thetvdb` is the sole
    # key TV Maze sends for this field, and accepting the field name too would
    # let a fabricated fixture pass while real payloads parse to None — which is
    # exactly how this went unnoticed through the original ingest.
    model_config = ConfigDict(extra="ignore")

    imdb: str | None = None
    tvdb: int | None = Field(None, alias="thetvdb")
    tvrage: int | None = None


class TVMazeEmbedded(BaseModel):
    model_config = ConfigDict(extra="ignore")

    episodes: list[TVMazeEpisode] = Field(default_factory=list)
    seasons: list[TVMazeSeason] = Field(default_factory=list)
    cast: list[TVMazeCastEntry] = Field(default_factory=list)
    crew: list[TVMazeCrewEntry] = Field(default_factory=list)


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
    rating: TVMazeRating | None = None
    embedded: TVMazeEmbedded = Field(default_factory=TVMazeEmbedded, alias="_embedded")

    @property
    def rating_average(self) -> float | None:
        return self.rating.average if self.rating else None
