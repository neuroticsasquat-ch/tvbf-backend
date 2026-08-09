"""Pydantic shapes for parsing the TMDB API — the `tvmaze/api_payloads.py` role.

Every shape here was **measured against the live API on 2026-08-09**, not taken
from documentation, using the same 11-namespace `append_to_response` list the
coverage audit decided (`docs/specs/NEU-1031-tmdb-coverage-audit.md` §1). Four
of those measurements are load-bearing enough to state up front.

**An appended `season/N` block carries no `id`.** The keys are `_id`, `air_date`,
`episodes`, `name`, `networks`, `overview`, `poster_path`, `season_number`,
`vote_average` — and `_id` is TMDB-internal, not the season id. The standalone
`GET /tv/{id}/season/{n}` response *does* carry `id`. So a season's `tmdb_id` has
to come from `seasons[]` on the series payload, matched by `season_number`; see
`TMDBSeasonDetail.tmdb_id`, which is optional for exactly this reason.

**Dates arrive as empty strings as well as nulls.** The `OptionalDate` /
`BeforeValidator` pattern ports straight from TV Maze. There is no `OptionalTime`
twin: TMDB carries no time component anywhere, which is the audit's one new known
loss (§6).

**A missing namespace is not an empty namespace.** Every appended namespace is
typed `X | None` and defaults to `None`, so a payload fetched without
`alternative_titles` parses to `None` rather than `[]`. The writers key off that
distinction — `None` means "the caller did not ask", and replacing a show's AKAs
with nothing on that basis would silently empty the table. It is the same trap
`upsert_show_payload(prune_seasons=...)` exists to avoid, one level out.

**Aliases are not accompanied by `populate_by_name`.** A field named for the
column it lands in (`tmdb_id`) reads from the upstream key (`id`) and *only* from
it, so a fixture that spells the upstream key wrong fails here rather than
parsing to `None` and looking like missing data — the failure mode
`TVMazeExternals` documents from the original ingest.

Credits — `aggregate_credits`, and episode `crew` / `guest_stars` — are
deliberately absent. `catalog` has no credit tables until NEU-1038, and a parser
with nowhere to write is a shape nobody validates.
"""

from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _empty_to_none(v: Any) -> Any:
    """`""` means "unknown" upstream, in the same places `null` does."""
    if v == "":
        return None
    return v


# Defined here rather than imported from `tvmaze/api_payloads.py`: that module
# retires with the schema it parses, and importing from it would make the
# replacement depend on the thing being replaced.
OptionalDate = Annotated[date | None, BeforeValidator(_empty_to_none)]
# `videos[].published_at` is the only timestamp TMDB returns, and it is nested
# inside the series payload — so a blank one would fail the *whole show's* parse,
# not just the field. Same coercion, different type; still no `OptionalTime`,
# because nothing upstream carries a bare time.
OptionalDateTime = Annotated[datetime | None, BeforeValidator(_empty_to_none)]
# Applied to identifiers and URLs, where `""` is a value that would be *used* —
# an empty `imdb_id` is a mapping key that matches nothing but is not null, and
# `/find/` would be called with it.
OptionalStr = Annotated[str | None, BeforeValidator(_empty_to_none)]


class _Payload(BaseModel):
    """Upstream shapes ignore unknown keys — TMDB adds fields without warning,
    and `softcore` (audit §3, skipped) is already one we do not model."""

    model_config = ConfigDict(extra="ignore")


class TMDBResults[T](_Payload):
    """The `{"results": [...]}` envelope most appended namespaces arrive in."""

    results: list[T] = Field(default_factory=list)


class TMDBRef(_Payload):
    """An `{id, name}` pair — genres and keywords are both exactly this."""

    tmdb_id: int = Field(alias="id")
    name: str


class TMDBCompany(_Payload):
    """A network or a production company. TMDB returns the same four keys for
    both, and `catalog` keeps them in separate tables because they are separate
    concepts, not because they are shaped differently."""

    tmdb_id: int = Field(alias="id")
    name: str
    logo_path: OptionalStr = None
    origin_country: OptionalStr = None


class TMDBProductionCountry(_Payload):
    iso_3166_1: str
    name: str | None = None


class TMDBSpokenLanguage(_Payload):
    """The only place TMDB names a language — `languages[]` and
    `original_language` are bare codes."""

    iso_639_1: str
    english_name: str | None = None
    name: str | None = None


class TMDBCreatedBy(_Payload):
    """A show-creator credit, returned denormalised: the person's name, gender
    and profile path ride inline rather than as a reference."""

    tmdb_person_id: int = Field(alias="id")
    credit_id: OptionalStr = None
    name: str
    original_name: OptionalStr = None
    gender: int | None = None
    profile_path: OptionalStr = None


class TMDBEpisode(_Payload):
    """One episode, identical in shape wherever it appears — inside a season's
    `episodes[]`, and as `last_episode_to_air` / `next_episode_to_air`."""

    tmdb_id: int = Field(alias="id")
    season_number: int
    episode_number: int
    name: str | None = None
    overview: str | None = None
    air_date: OptionalDate = None
    runtime: int | None = None
    still_path: OptionalStr = None
    production_code: OptionalStr = None
    # `premiere` / `mid_season` / `finale` / `standard` — upstream's vocabulary,
    # stored verbatim, which is what makes finale detection heuristic-free.
    episode_type: OptionalStr = None
    vote_average: float | None = None
    vote_count: int | None = None


class TMDBSeasonSummary(_Payload):
    """An entry of the series payload's `seasons[]`.

    **This is where a season's identity comes from.** It is the only place TMDB
    states a season's `id` alongside its `season_number`; the appended `season/N`
    block that carries the episodes does not.
    """

    tmdb_id: int = Field(alias="id")
    season_number: int
    name: str | None = None
    overview: str | None = None
    poster_path: OptionalStr = None
    air_date: OptionalDate = None
    vote_average: float | None = None
    episode_count: int | None = None


class TMDBSeasonDetail(_Payload):
    """A season with its episodes — an appended `season/N`, or `get_tv_season`.

    `tmdb_id` is optional **because the appended form omits it** (measured). Seasons
    are therefore matched to their `seasons[]` summary by `season_number`, which
    the appended form does carry.
    """

    tmdb_id: int | None = Field(default=None, alias="id")
    season_number: int
    name: str | None = None
    overview: str | None = None
    poster_path: OptionalStr = None
    air_date: OptionalDate = None
    vote_average: float | None = None
    # Present on the season payload and absent from the ticket's inventory
    # (audit §8) — the series-level field at season grain.
    networks: list[TMDBCompany] = Field(default_factory=list)
    episodes: list[TMDBEpisode] = Field(default_factory=list)


class TMDBExternalIds(_Payload):
    """Richer than the migration needs — `tvdb_id` and `imdb_id` carry the
    mapping tiers, the rest are stored because they ride the same request."""

    imdb_id: OptionalStr = None
    tvdb_id: int | None = None
    tvrage_id: int | None = None
    wikidata_id: OptionalStr = None
    freebase_id: OptionalStr = None
    freebase_mid: OptionalStr = None
    facebook_id: OptionalStr = None
    instagram_id: OptionalStr = None
    twitter_id: OptionalStr = None


class TMDBAlternativeTitle(_Payload):
    """An **AKA** — `CONTEXT.md`'s word for the concept, and what the table is
    called. The class keeps the upstream name because it is a parser."""

    iso_3166_1: str | None = None
    title: str
    type: str | None = None


class TMDBContentRating(_Payload):
    iso_3166_1: str
    rating: str | None = None
    descriptors: list[str] = Field(default_factory=list)


class TMDBEpisodeGroup(_Payload):
    """A group header. The group's actual episode assignments live behind a
    separate `/tv/episode_group/{id}` request that `append_to_response` cannot
    ride, so headers are the whole of what this namespace offers."""

    # A hex string, not an integer, unlike every other TMDB id here.
    tmdb_id: str = Field(alias="id")
    name: str | None = None
    description: str | None = None
    episode_count: int | None = None
    group_count: int | None = None
    type: int | None = None
    network: TMDBCompany | None = None


class TMDBImage(_Payload):
    """TMDB gives an image no id; the path is its identity."""

    file_path: str
    aspect_ratio: float | None = None
    height: int | None = None
    width: int | None = None
    iso_639_1: OptionalStr = None
    vote_average: float | None = None
    vote_count: int | None = None


class TMDBImages(_Payload):
    backdrops: list[TMDBImage] = Field(default_factory=list)
    logos: list[TMDBImage] = Field(default_factory=list)
    posters: list[TMDBImage] = Field(default_factory=list)


class TMDBTranslationData(_Payload):
    name: str | None = None
    overview: str | None = None
    tagline: str | None = None
    homepage: OptionalStr = None


class TMDBTranslation(_Payload):
    """A locale is a language *and* a country: TMDB returns `pt-BR` and `pt-PT`
    as separate entries."""

    iso_639_1: str
    iso_3166_1: str
    # How the locale names itself, and its English name — not the translated
    # show title, which is `data.name`.
    name: str | None = None
    english_name: str | None = None
    data: TMDBTranslationData = Field(default_factory=TMDBTranslationData)


class TMDBTranslations(_Payload):
    """The one namespace that does not use `results`."""

    translations: list[TMDBTranslation] = Field(default_factory=list)


class TMDBVideo(_Payload):
    tmdb_id: str = Field(alias="id")
    # The id *within* the site — a YouTube watch id, say.
    key: OptionalStr = None
    name: str | None = None
    site: str | None = None
    size: int | None = None
    type: str | None = None
    official: bool | None = None
    published_at: OptionalDateTime = None
    iso_639_1: OptionalStr = None
    iso_3166_1: OptionalStr = None


class TMDBWatchProviderOffer(_Payload):
    provider_id: int
    provider_name: str
    logo_path: OptionalStr = None
    display_priority: int | None = None


class TMDBWatchProviderCountry(_Payload):
    """One country's offers, keyed by the kind of offer.

    The five keys are the whole vocabulary, which is why `show_watch_provider`
    can carry a CHECK on `offer_type` where `show.status` cannot: these are keys
    of a response object we read, not free text upstream invents.
    """

    link: OptionalStr = None
    flatrate: list[TMDBWatchProviderOffer] = Field(default_factory=list)
    buy: list[TMDBWatchProviderOffer] = Field(default_factory=list)
    rent: list[TMDBWatchProviderOffer] = Field(default_factory=list)
    free: list[TMDBWatchProviderOffer] = Field(default_factory=list)
    ads: list[TMDBWatchProviderOffer] = Field(default_factory=list)

    def offers(self) -> list[tuple[str, TMDBWatchProviderOffer]]:
        """`(offer_type, offer)` pairs, in the order `show_watch_provider`'s
        CHECK constraint names them."""
        return [
            (offer_type, offer)
            for offer_type in ("flatrate", "buy", "rent", "free", "ads")
            for offer in getattr(self, offer_type)
        ]


class TMDBWatchProviders(_Payload):
    """`results` is a map of country code to offers, not a list."""

    results: dict[str, TMDBWatchProviderCountry] = Field(default_factory=dict)


class TMDBScreenedEpisode(_Payload):
    """An entry of `screened_theatrically` — `id` is the *episode's* id, which
    is what lets the flag land on the episode row without a season lookup."""

    tmdb_id: int = Field(alias="id")
    season_number: int | None = None
    episode_number: int | None = None


class TMDBSeries(_Payload):
    """A `GET /tv/{id}` response, with whatever `append_to_response` rode along.

    Every appended namespace is `None` until it is asked for. Nothing here
    distinguishes "TMDB returned an empty list" from "TMDB returned nothing" by
    accident — that is the distinction the writers spend.
    """

    tmdb_id: int = Field(alias="id")
    name: str
    original_name: str | None = None
    overview: str | None = None
    tagline: str | None = None
    homepage: OptionalStr = None
    type: str | None = None
    adult: bool = False
    # Verbatim, untranslated: `Returning Series` / `Planned` / `In Production` /
    # `Ended` / `Canceled` (audit D1). `is_ended` is generated in the database
    # from it, so nothing here writes that.
    status: str | None = None
    in_production: bool | None = None

    first_air_date: OptionalDate = None
    last_air_date: OptionalDate = None

    original_language: OptionalStr = None
    languages: list[str] = Field(default_factory=list)
    spoken_languages: list[TMDBSpokenLanguage] = Field(default_factory=list)
    origin_country: list[str] = Field(default_factory=list)
    production_countries: list[TMDBProductionCountry] = Field(default_factory=list)

    popularity: float | None = None
    vote_average: float | None = None
    vote_count: int | None = None

    poster_path: OptionalStr = None
    backdrop_path: OptionalStr = None

    # Both exclude season 0 — measured, and the convention audit D2 adopts for
    # our own completion math.
    number_of_episodes: int | None = None
    number_of_seasons: int | None = None
    # Usually `[]`: 6 of 7 sampled shows returned nothing here, which is why the
    # scalar `runtime` is derived from episodes instead (audit D4).
    episode_run_time: list[int] = Field(default_factory=list)

    genres: list[TMDBRef] = Field(default_factory=list)
    networks: list[TMDBCompany] = Field(default_factory=list)
    production_companies: list[TMDBCompany] = Field(default_factory=list)
    created_by: list[TMDBCreatedBy] = Field(default_factory=list)
    seasons: list[TMDBSeasonSummary] = Field(default_factory=list)

    last_episode_to_air: TMDBEpisode | None = None
    next_episode_to_air: TMDBEpisode | None = None

    external_ids: TMDBExternalIds | None = None
    alternative_titles: TMDBResults[TMDBAlternativeTitle] | None = None
    content_ratings: TMDBResults[TMDBContentRating] | None = None
    episode_groups: TMDBResults[TMDBEpisodeGroup] | None = None
    images: TMDBImages | None = None
    keywords: TMDBResults[TMDBRef] | None = None
    screened_theatrically: TMDBResults[TMDBScreenedEpisode] | None = None
    translations: TMDBTranslations | None = None
    videos: TMDBResults[TMDBVideo] | None = None
    # The one namespace whose key is not a valid Python identifier.
    watch_providers: TMDBWatchProviders | None = Field(default=None, alias="watch/providers")

    # Appended `season/N` blocks, lifted out of the dynamic keys they arrive
    # under. Never populated from a key of its own — see the validator.
    appended_seasons: list[TMDBSeasonDetail] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _collect_appended_seasons(cls, data: Any) -> Any:
        """Gather `season/1`, `season/2`, … into one list.

        `append_to_response` returns each season under its own dynamic key, so
        no static field can name them. Anything a caller passed as
        `appended_seasons` is discarded rather than merged: the response is the
        only authority on what rode along, and quietly honouring both would make
        a typo'd key look like a season that was not requested.
        """
        if not isinstance(data, dict):
            return data
        seasons = [v for k, v in data.items() if isinstance(k, str) and k.startswith("season/")]
        if not seasons and "appended_seasons" not in data:
            return data
        return {**data, "appended_seasons": [s for s in seasons if isinstance(s, dict)]}
