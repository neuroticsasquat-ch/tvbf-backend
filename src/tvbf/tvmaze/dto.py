from dataclasses import dataclass, field
from datetime import date, time

from pydantic import BaseModel, ConfigDict

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


@dataclass
class ShowFilters:
    search: str | None = None
    status: str | None = None
    genres: list[str] = field(default_factory=list)
    network_ids: list[int] = field(default_factory=list)
    language: str | None = None
    type: str | None = None


class NetworkRef(BaseModel):
    """Compact network/web-channel reference used inside shows and seasons."""

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


def build_show_summary(
    show, genre_names: list[str], network: NetworkRef | None, web_channel: NetworkRef | None
) -> ShowSummary:
    return ShowSummary(
        id=show.id,
        name=show.name,
        type=show.type,
        status=show.status,
        language=show.language,
        premiered=show.premiered,
        ended=show.ended,
        image_medium=show.image_medium,
        image_original=show.image_original,
        network=network,
        web_channel=web_channel,
        genres=sorted(genre_names),
    )


def build_show_detail(show, seasons, genres, network, web_channel) -> "ShowDetail":
    # Season-level network/web-channel refs are intentionally left as None in v1.
    # Season rows carry network_id / web_channel_id FKs; a future refactor can
    # populate them from a batch lookup when a UI actually needs them.
    season_dtos: list[SeasonOut] = [
        SeasonOut(
            id=s.id,
            number=s.number,
            name=s.name,
            episode_order=s.episode_order,
            premiere_date=s.premiere_date,
            end_date=s.end_date,
            network=None,
            web_channel=None,
            image_medium=s.image_medium,
            image_original=s.image_original,
            summary=s.summary,
        )
        for s in seasons
    ]

    return ShowDetail(
        id=show.id,
        name=show.name,
        type=show.type,
        status=show.status,
        language=show.language,
        premiered=show.premiered,
        ended=show.ended,
        image_medium=show.image_medium,
        image_original=show.image_original,
        network=NetworkRef(id=network.id, name=network.name) if network else None,
        web_channel=NetworkRef(id=web_channel.id, name=web_channel.name) if web_channel else None,
        genres=[g.name for g in genres],
        summary=show.summary,
        runtime=show.runtime,
        official_site=show.official_site,
        externals=ExternalsOut(
            imdb=show.externals_imdb,
            tvdb=show.externals_tvdb,
            tvrage=show.externals_tvrage,
        )
        if (show.externals_imdb or show.externals_tvdb or show.externals_tvrage)
        else None,
        tvmaze_updated=show.tvmaze_updated,
        seasons=season_dtos,
    )
