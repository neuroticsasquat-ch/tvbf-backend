"""The public API's response shapes, and the one place a `catalog` row becomes one.

**The contract does not change at cutover** (NEU-1047): same routes, same keys,
same ids, because NEU-1042 preserved TV Maze's ids as the catalog surrogates.
What changes is where each key is read from, and this module is the whole of
that translation — every column rename, every merged concept and every field
TMDB has no counterpart for is decided here rather than at a call site.

The module moved from `tvmaze/schemas.py` unchanged in shape. It lives under
`catalog/` now because the API is served from the source-neutral spine (ADR-0007)
and leaving the contract inside the package NEU-1050 retires would mean moving it
twice.

**The renames are mechanical; the six judgement calls are not.**

* `language` reads `original_language` — the only one of TMDB's three language
  concepts with exactly one value per show, which the filter's exact-match
  semantics require (audit D3). The *values* change shape with it: "English"
  becomes "en". NEU-1037 is the frontend's half of that.
* `ended` reads `last_air_date` **only for a show that has ended**. TMDB's field
  is the last episode's air date and is populated for running shows too, where
  TV Maze's `ended` was null until a show finished. `is_ended` is the generated
  column that decides it, so `Canceled` counts as ended and this cannot drift
  from `status`.
* `network` collapses two fields into one. TMDB draws no distinction between a
  broadcaster and a streamer (audit §6), so `catalog.network` absorbs both and
  **`web_channel` is now always null** — the key stays in the payload because
  removing it is a contract change, and the SPA renders only `network` anyway.
  Which network, when a show has several, is decided in `browse_queries`.
* `tvmaze_updated` keeps its name and becomes *when we last mirrored this show*
  — the epoch of `tmdb_synced_at`, or of `ingested_at` for a row no full pass has
  covered. The name is a legacy alias: it is a live sort key (`?sort=`) and an
  SPA type, so renaming it is a coordinated change across two repos for a field
  whose meaning — "recently updated" — survives intact.
* `gender` translates TMDB's integer enum back to the words TV Maze sent, because
  the SPA renders the value verbatim. `Non-Binary` is the one new word: TV Maze
  said `Other`, TMDB says non-binary specifically, and passing the integer
  through would put a `3` on a person page.
* `end_date`, `airtime`, `country_name`, `timezone`, `birthday` and `deathday`
  have **no TMDB counterpart at all** and are permanently null. Each is a
  documented loss (audit §6, and `catalog.models.Person`), not an oversight, and
  each field stays in the payload for the same reason `web_channel` does.

`CharacterRef.image_medium` joins that list for a different reason: TMDB models a
character as free text, so `catalog.character` has no image column at all.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field

from tvbf.catalog import models as m
from tvbf.catalog.episodes import public_number
from tvbf.catalog.images import PROFILE, medium_url, poster_urls, profile_urls, still_urls

ALLOWED_SORT_KEYS = {
    "name",
    "-name",
    "premiered",
    "-premiered",
    "tvmaze_updated",
    "-tvmaze_updated",
    "last_aired",
    "-last_aired",
}

# TMDB's integer gender enum, in the vocabulary TV Maze used and the SPA renders.
# 0 is "not specified" and reads as unknown, exactly like the 61,635 null-gender
# rows the TV Maze mirror already carried.
_GENDER_NAMES = {1: "Female", 2: "Male", 3: "Non-Binary"}


@dataclass
class ShowFilters:
    search: str | None = None
    status: str | None = None
    genres: list[str] = field(default_factory=list)
    network_ids: list[int] = field(default_factory=list)
    language: str | None = None
    type: str | None = None


class NetworkRef(BaseModel):
    """Compact network reference. Also the type of the retired `web_channel`
    key, which every payload now leaves null — see the module docstring."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class NetworkOut(BaseModel):
    """Full network representation for GET /networks."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    country_code: str | None = None
    country_name: str | None = None
    timezone: str | None = None


class GenreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class ExternalsOut(BaseModel):
    imdb: str | None = None
    tvdb: int | None = None
    tvrage: int | None = None


class SeasonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: int
    name: str | None = None
    episode_order: int | None = None
    premiere_date: date | None = None
    end_date: date | None = None
    network: NetworkRef | None = None
    web_channel: NetworkRef | None = None
    image_medium: str | None = None
    image_original: str | None = None
    summary: str | None = None


class EpisodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    show_id: int
    season_id: int | None = None
    season: int
    number: int | None = None
    name: str | None = None
    airdate: date | None = None
    airtime: time | None = None
    runtime: int | None = None
    summary: str | None = None
    image_medium: str | None = None
    image_original: str | None = None
    rating_average: float | None = None
    my_rating: float | None = None
    # Per-user watched flag. Populated by `/me/*` list endpoints so list rows
    # can render the watch checkbox without a per-show round trip. Null on
    # endpoints that have no user context (catalog browse).
    watched: bool | None = None


class ShowSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: str | None = None
    status: str | None = None
    language: str | None = None
    premiered: date | None = None
    ended: date | None = None
    image_medium: str | None = None
    image_original: str | None = None
    network: NetworkRef | None = None
    web_channel: NetworkRef | None = None
    genres: list[str] = []
    matched_aka: str | None = None
    rating_average: float | None = None
    my_rating: float | None = None


class ShowDetail(ShowSummary):
    summary: str | None = None
    runtime: int | None = None
    official_site: str | None = None
    externals: ExternalsOut | None = None
    tvmaze_updated: int
    seasons: list[SeasonOut] = []


class ShowListPage(BaseModel):
    items: list[ShowSummary]
    page: int
    per_page: int
    total: int
    total_pages: int


class TrendingShowOut(ShowSummary):
    """One entry of the trending snapshot: a `ShowSummary` **flattened**, plus
    the one field that makes it an entry rather than a search result (NEU-1056).

    Flattened rather than nested under a `show` key, on `RecommendationOut`'s
    reasoning: `ShowGrid` and `ShowCard` already take a `ShowSummary`, and a
    wrapper type would cost the frontend something for a single boolean.

    `in_my_shows` is a **mark, never a filter**. Trending is a claim about the
    world, and seeing a show you already track in it is a feature rather than
    noise — the surface renders it differently, it does not drop it.
    """

    in_my_shows: bool = False


class TrendingOut(BaseModel):
    """The `GET /trending` body.

    An object rather than a bare array, because the list is only half of what
    the surface needs: `captured_at` says when TMDB was asked, and the SPA is
    free to show it.

    **`captured_at` is null exactly when `shows` is empty**, and both are what a
    snapshot past the seven-day cutoff answers. It describes the list served, so
    there is no reading of the payload under which a client can recover the age
    of a snapshot the server withheld — which is what keeps the cutoff the
    server's rule alone (project spec §3).
    """

    captured_at: datetime | None = None
    shows: list[TrendingShowOut] = []


class PersonRef(BaseModel):
    """Compact person reference used inside credit payloads."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    image_medium: str | None = None


class CharacterRef(BaseModel):
    """Compact character reference used inside cast payloads."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    image_medium: str | None = None


class CastMemberOut(BaseModel):
    person: PersonRef
    character: CharacterRef
    # `self` is a Python keyword, so the attribute is `self_credit` and only the
    # serialized key matches upstream's naming.
    self_credit: bool = Field(False, serialization_alias="self")
    voice: bool = False


class CrewMemberOut(BaseModel):
    person: PersonRef
    role: str


class PersonOut(BaseModel):
    """A person, as returned by GET /people/{id} and as each item of a
    GET /people search page."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    country_code: str | None = None
    country_name: str | None = None
    birthday: date | None = None
    deathday: date | None = None
    gender: str | None = None
    image_medium: str | None = None
    image_original: str | None = None


class PersonListPage(BaseModel):
    """Paginated person search results. Items are the same shape as person
    detail — a person row is small, and one person shape keeps the frontend from
    needing a second fetch to render anything beyond a name."""

    items: list[PersonOut]
    page: int
    per_page: int
    total: int
    total_pages: int


class ShowRef(BaseModel):
    """Compact show reference used inside a person's filmography."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    image_medium: str | None = None
    premiered: date | None = None


class EpisodeRef(BaseModel):
    """Compact episode reference used inside episode-level credits. Carries
    season and number so the frontend can render "Show — S2E11" without a round
    trip."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str | None = None
    season: int
    number: int | None = None
    airdate: date | None = None


class PersonCastCreditOut(BaseModel):
    show: ShowRef
    character: CharacterRef
    self_credit: bool = Field(False, serialization_alias="self")
    voice: bool = False


class PersonCrewCreditOut(BaseModel):
    show: ShowRef
    role: str


class PersonGuestCreditOut(BaseModel):
    show: ShowRef
    episode: EpisodeRef
    character: CharacterRef
    self_credit: bool = Field(False, serialization_alias="self")
    voice: bool = False


class PersonEpisodeCrewCreditOut(BaseModel):
    show: ShowRef
    episode: EpisodeRef
    role: str


class PersonCreditsOut(BaseModel):
    """Grouped filmography. The four kinds are genuinely different shapes
    (person-as-character, person-in-function, person-as-character-in-one-episode,
    person-in-function-on-one-episode) and the page renders them as separate
    sections, so they stay grouped rather than interleaved. All four keys are
    always present — an absent category is an empty list, never a missing key."""

    cast: list[PersonCastCreditOut] = []
    crew: list[PersonCrewCreditOut] = []
    guest_cast: list[PersonGuestCreditOut] = []
    episode_crew: list[PersonEpisodeCrewCreditOut] = []


# ---------------------------------------------------------------------------
# Row -> payload builders
# ---------------------------------------------------------------------------
#
# Every one of these existed before the repoint; what changed is that none of
# them can be `model_validate(row)` any more. A catalog row's attributes are
# TMDB's names, and Pydantic's `from_attributes` silently falls back to a field's
# default for an attribute that is missing — so a bare `model_validate` would
# emit an `EpisodeOut` with a null `airdate` rather than fail, and the payload
# would look plausible and be empty. Naming each field is what makes a rename a
# type error instead.


def _updated_epoch(show: m.Show) -> int:
    """`tvmaze_updated`: when a full TMDB payload last landed on this row.

    Falls back to `ingested_at` for a row no full pass has covered — a copied row
    before the ingest reached it, or a locally-authored one, which has no
    `tmdb_synced_at` and never will (ADR-0008).
    """
    stamp = show.tmdb_synced_at or show.ingested_at
    return int(stamp.timestamp())


def build_network_out(network: m.Network) -> NetworkOut:
    """`GET /networks`. `country_name` and `timezone` have no TMDB counterpart."""
    return NetworkOut(
        id=network.id,
        name=network.name,
        country_code=network.origin_country,
        country_name=None,
        timezone=None,
    )


def build_network_ref(network: m.Network | None) -> NetworkRef | None:
    return NetworkRef(id=network.id, name=network.name) if network is not None else None


def build_season_out(season: m.Season) -> SeasonOut:
    """One season row.

    `network` / `web_channel` stay null here as they always have — season rows
    carry the FKs (`catalog.season_network`) but no UI has ever asked for them.
    `end_date` is null because TMDB models only `air_date`; deriving it from the
    season's last episode would be inventing a date upstream never gave.
    """
    medium, original = poster_urls(season.poster_path)
    return SeasonOut(
        id=season.id,
        number=season.season_number,
        name=season.name,
        episode_order=season.episode_count,
        premiere_date=season.air_date,
        end_date=None,
        network=None,
        web_channel=None,
        image_medium=medium,
        image_original=original,
        summary=season.overview,
    )


def build_episode_out(
    episode: m.Episode,
    *,
    my_rating: float | None = None,
    watched: bool | None = None,
) -> EpisodeOut:
    """One episode row. `airtime` is null — TMDB carries no time component."""
    medium, original = still_urls(episode.still_path)
    return EpisodeOut(
        id=episode.id,
        show_id=episode.show_id,
        season_id=episode.season_id,
        season=episode.season_number,
        number=public_number(episode),
        name=episode.name,
        airdate=episode.air_date,
        airtime=None,
        runtime=episode.runtime,
        summary=episode.overview,
        image_medium=medium,
        image_original=original,
        rating_average=(float(episode.vote_average) if episode.vote_average is not None else None),
        my_rating=my_rating,
        watched=watched,
    )


def build_show_summary(
    show: m.Show,
    genre_names: list[str],
    network: NetworkRef | None,
    web_channel: NetworkRef | None = None,
    matched_aka: str | None = None,
    my_rating: float | None = None,
) -> ShowSummary:
    """A show list row.

    `web_channel` is a parameter only so the shape of the call site is unchanged;
    every caller passes nothing, because TMDB merged the concept into `network`.
    """
    medium, original = poster_urls(show.poster_path)
    return ShowSummary(
        id=show.id,
        name=show.name,
        type=show.type,
        status=show.status,
        language=show.original_language,
        premiered=show.first_air_date,
        ended=show.last_air_date if show.is_ended else None,
        image_medium=medium,
        image_original=original,
        network=network,
        web_channel=web_channel,
        genres=sorted(genre_names),
        matched_aka=matched_aka,
        rating_average=float(show.vote_average) if show.vote_average is not None else None,
        my_rating=my_rating,
    )


def build_show_detail(
    show: m.Show,
    seasons: list[m.Season],
    genres: list[m.Genre],
    network: m.Network | None,
    my_rating: float | None = None,
) -> ShowDetail:
    medium, original = poster_urls(show.poster_path)
    return ShowDetail(
        id=show.id,
        name=show.name,
        type=show.type,
        status=show.status,
        language=show.original_language,
        premiered=show.first_air_date,
        ended=show.last_air_date if show.is_ended else None,
        image_medium=medium,
        image_original=original,
        network=build_network_ref(network),
        web_channel=None,
        genres=[g.name for g in genres],
        summary=show.overview,
        runtime=show.runtime,
        official_site=show.homepage,
        externals=ExternalsOut(
            imdb=show.imdb_id,
            tvdb=show.tvdb_id,
            tvrage=show.tvrage_id,
        )
        if (show.imdb_id or show.tvdb_id or show.tvrage_id)
        else None,
        tvmaze_updated=_updated_epoch(show),
        seasons=[build_season_out(s) for s in seasons],
        rating_average=float(show.vote_average) if show.vote_average is not None else None,
        my_rating=my_rating,
    )


def build_person_ref(person: m.Person) -> PersonRef:
    return PersonRef(
        id=person.id,
        name=person.name,
        image_medium=medium_url(person.profile_path, PROFILE),
    )


def build_person_out(person: m.Person) -> PersonOut:
    """A person row. Four fields are permanently null — see the module docstring."""
    medium, original = profile_urls(person.profile_path)
    return PersonOut(
        id=person.id,
        name=person.name,
        country_code=None,
        country_name=None,
        birthday=None,
        deathday=None,
        gender=_GENDER_NAMES.get(person.gender) if person.gender is not None else None,
        image_medium=medium,
        image_original=original,
    )


def build_show_ref(show: m.Show) -> ShowRef:
    return ShowRef(
        id=show.id,
        name=show.name,
        image_medium=poster_urls(show.poster_path)[0],
        premiered=show.first_air_date,
    )


def build_episode_ref(episode: m.Episode) -> EpisodeRef:
    return EpisodeRef(
        id=episode.id,
        name=episode.name,
        season=episode.season_number,
        number=public_number(episode),
        airdate=episode.air_date,
    )


def build_character_ref(character: m.Character) -> CharacterRef:
    """`image_medium` is permanently null: TMDB models a character as free text,
    so `catalog.character` has no image column to compose one from."""
    return CharacterRef(id=character.id, name=character.name, image_medium=None)


def build_cast_member(person: m.Person, character: m.Character) -> CastMemberOut:
    """`self` and `voice` are permanently false — TMDB flags neither on a credit,
    where TV Maze carried both booleans."""
    return CastMemberOut(
        person=build_person_ref(person),
        character=build_character_ref(character),
        self_credit=False,
        voice=False,
    )


def build_crew_member(person: m.Person, role: m.CrewRole) -> CrewMemberOut:
    """`role` is the job. `catalog.crew_role` splits TV Maze's single role name
    into `(department, job)`; the job is the half that reads as a credit."""
    return CrewMemberOut(person=build_person_ref(person), role=role.job)


def build_person_credits(cast_rows, crew_rows, guest_rows, episode_crew_rows) -> PersonCreditsOut:
    return PersonCreditsOut(
        cast=[
            PersonCastCreditOut(
                show=build_show_ref(show),
                character=build_character_ref(character),
                self_credit=False,
                voice=False,
            )
            for show, character in cast_rows
        ],
        crew=[
            PersonCrewCreditOut(show=build_show_ref(show), role=role.job)
            for show, role in crew_rows
        ],
        guest_cast=[
            PersonGuestCreditOut(
                show=build_show_ref(show),
                episode=build_episode_ref(episode),
                character=build_character_ref(character),
                self_credit=False,
                voice=False,
            )
            for episode, show, character in guest_rows
        ],
        episode_crew=[
            PersonEpisodeCrewCreditOut(
                show=build_show_ref(show),
                episode=build_episode_ref(episode),
                role=role.job,
            )
            for episode, show, role in episode_crew_rows
        ],
    )
