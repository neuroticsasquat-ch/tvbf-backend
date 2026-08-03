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


def _id_from_href(href: str | None) -> int | None:
    """Trailing path segment of a TV Maze self-link, as an int."""
    if not href:
        return None
    tail = href.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


class TVMazeGuestCastCredit(BaseModel):
    """One guest-cast credit as returned by `/people/{id}?embed[]=guestcastcredits`.

    Unlike the show-side cast embed, guest credits carry `_links` rather than
    embedded objects, so the episode and character ids are parsed out of href
    strings. Both are optional: a credit missing either link is unusable and the
    upsert skips it rather than guessing.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    is_self: bool = Field(False, alias="self")
    is_voice: bool = Field(False, alias="voice")
    links: dict = Field(default_factory=dict, alias="_links")

    @property
    def episode_id(self) -> int | None:
        return _id_from_href((self.links.get("episode") or {}).get("href"))

    @property
    def character_id(self) -> int | None:
        return _id_from_href((self.links.get("character") or {}).get("href"))

    @property
    def character_name(self) -> str | None:
        return (self.links.get("character") or {}).get("name")


class TVMazePersonEmbedded(BaseModel):
    """Only `guestcastcredits` is modelled, because only it is requested.

    `castcredits` and `crewcredits` embed fine but duplicate what the show axis
    already writes, and person-side credits carry no ordering — writing them
    would clobber the billing order pass A captured from the show side.
    """

    model_config = ConfigDict(extra="ignore")

    guestcastcredits: list[TVMazeGuestCastCredit] = Field(default_factory=list)


class TVMazePersonDetail(TVMazePerson):
    """A person fetched directly from `/people/{id}`, with its embeds."""

    embedded: TVMazePersonEmbedded = Field(default_factory=TVMazePersonEmbedded, alias="_embedded")


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
