"""Idempotent writes from a TMDB payload into `catalog`.

The `tvmaze/upsert.py` role, and deliberately the same shape: one entry point
per complete payload, a caller that owns the transaction, and a re-run that is a
no-op. Four things read differently, and each is a decision rather than a port.

**Every conflict target is `tmdb_id`, never the primary key.** Under TV Maze the
two were the same column. Here the primary key is an internal surrogate
(ADR-0008) that user data references and that the migration seeds from the old
TV Maze ids, so ingest must never write one — it writes `tmdb_id` and reads the
surrogate back with `RETURNING`.

**A locally-authored row is untouchable, and structurally so.** `tmdb_id IS NULL`
marks a row nobody upstream owns — the sanctioned way to hold a show TMDB does
not list. Postgres treats NULLs as distinct in a unique index, so an
`ON CONFLICT (tmdb_id)` can never match one: the protection is a property of the
index rather than a predicate someone has to remember. The season prune, which
deletes rather than upserts, needs the guard written out — see
`prune_missing_seasons`.

**This module writes `catalog`, and lives under `tmdb/` anyway.** `catalog` is
source-neutral by design (ADR-0007) and holds no knowledge of who filled it; the
mapping from one upstream's payload to those tables belongs to that upstream.

**Namespaces are authoritative only when they were requested.** Every appended
namespace parses to `None` when absent (see `api_payloads`), and every writer
here no-ops on `None` rather than treating it as an empty list. Replacing a
show's AKAs because the caller did not ask for them is the same failure
`prune_seasons` exists to prevent.

Credits are NEU-1038's: `catalog` has no person, character or cast tables yet,
so `aggregate_credits` and episode `guest_stars` / `crew` are parsed by nobody
and written by nobody.
"""

import logging
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import Float, cast, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.catalog import models as m
from tvbf.tmdb.api_payloads import (
    TMDBCompany,
    TMDBCreatedBy,
    TMDBEpisode,
    TMDBSeasonDetail,
    TMDBSeries,
    TMDBWatchProviders,
)

log = logging.getLogger(__name__)

# Postgres caps bind parameters per query at 32,767 — the ceiling that forced
# `tvmaze.upsert._EPISODE_BATCH_SIZE`, and it has not moved. An episode binds 15
# columns here, so 1,000 rows is 15,000 parameters: comfortably under, and still
# under for every other table in this module, none of which binds more. Without
# batching, any show past ~2,180 episodes — soaps, daily talk, news — fails
# outright.
_BATCH_SIZE = 1000


def _dec(value: float | None) -> Decimal | None:
    """Float to `Numeric`. Via `str` so 8.9 stores as 8.9 rather than 8.9000001."""
    return None if value is None else Decimal(str(value))


async def refresh_runtime(session: AsyncSession, *, show_id: int) -> int | None:
    """Recompute the show's scalar `runtime` from its mirrored episodes (audit D4).

    TMDB's own series-level `episode_run_time` was empty for 6 of 7 sampled
    shows, while episode `runtime` is populated for 94.9% of episodes — so the
    derived value is both more available and more accurate. Median rather than
    mean so one feature-length finale cannot drag a 22-minute sitcom upward.

    **Computed from the table, not from the payload in hand.** The audit calls
    this an ordering constraint rather than a detail: the value cannot be
    finalised until every season is mirrored, including `get_tv_season`
    overflow, and it has to be recomputed whenever a delta adds episodes. A
    median taken over one call's episodes would let a delta carrying 2 of 20
    seasons overwrite the full pass's answer with a worse one — staleness the
    audit is willing to accept, a wrong value it is not.

    Season 0 is excluded, because D2 excludes specials from every other piece of
    completion math and a 4-minute webisode is not this show's runtime.

    A show with no episode runtimes at all leaves the stored value alone rather
    than blanking it, and returns `None`.
    """
    median_runtime = (
        await session.execute(
            select(
                func.round(
                    func.percentile_cont(0.5).within_group(cast(m.Episode.runtime, Float).asc())
                )
            ).where(
                m.Episode.show_id == show_id,
                m.Episode.season_number > 0,
                m.Episode.runtime.is_not(None),
            )
        )
    ).scalar_one_or_none()
    if median_runtime is None:
        return None
    runtime = int(median_runtime)
    await session.execute(update(m.Show).where(m.Show.id == show_id).values(runtime=runtime))
    return runtime


async def mark_series_synced(session: AsyncSession, *, show_id: int) -> None:
    """Stamp the show as fully mirrored — the ingest's resumability watermark.

    Kept out of `upsert_series_payload` on purpose. That function writes
    whatever payload it is handed, including the narrower ones a delta or a
    single-season re-fetch produces; this asserts something stronger, that a
    *complete* pass covered the show, and only the caller doing the complete
    pass can say so. `mark_akas_synced` splits the same way for the same reason.

    Load-bearing because the work list is `tmdb_synced_at IS NULL` rather than
    "no row exists": the migration copied ~89k TV Maze shows into `catalog` and
    mapped a `tmdb_id` onto most of them, so row-exists would skip precisely the
    shows users track. See `tvbf.tmdb.ingest`.
    """
    await session.execute(
        update(m.Show).where(m.Show.id == show_id).values(tmdb_synced_at=func.now())
    )


async def _upsert_by_tmdb_id(
    session: AsyncSession, model: Any, rows: list[dict[str, Any]]
) -> dict[Any, int]:
    """Upsert lookup rows on their `tmdb_id`, returning `{tmdb_id: surrogate id}`.

    One statement per table rather than one per row: a show names a few dozen
    genres, networks, companies, keywords and providers between them, and at 20
    requests a second that is the difference between a handful of round trips
    per show and several hundred.

    Rows must share a key set — the update clause is built from the first.
    """
    if not rows:
        return {}
    # The same network can appear on the series and on several of its seasons.
    deduped = {r["tmdb_id"]: r for r in rows}
    values = list(deduped.values())
    stmt = insert(model).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[model.tmdb_id],
        set_={c: getattr(stmt.excluded, c) for c in values[0] if c != "tmdb_id"},
    ).returning(model.tmdb_id, model.id)
    result = await session.execute(stmt)
    return {row.tmdb_id: row.id for row in result.all()}


async def _ensure_countries(session: AsyncSession, entries: dict[str, str | None]) -> None:
    """Insert any country codes we have not seen, keeping the best name known.

    `origin_country[]`, `content_ratings[]` and `watch/providers` supply bare
    codes; only `production_countries[]` names them. So the update coalesces:
    a payload arriving with no name must not blank one an earlier payload gave.
    """
    if not entries:
        return
    rows = [{"iso_3166_1": code, "name": name} for code, name in entries.items()]
    stmt = insert(m.Country).values(rows)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[m.Country.iso_3166_1],
            set_={"name": func.coalesce(stmt.excluded.name, m.Country.name)},
        )
    )


async def _ensure_languages(
    session: AsyncSession, entries: dict[str, tuple[str | None, str | None]]
) -> None:
    """The same, for language codes. `languages[]` and `original_language` are
    bare codes; only `spoken_languages[]` names them."""
    if not entries:
        return
    rows = [
        {"iso_639_1": code, "english_name": english, "name": name}
        for code, (english, name) in entries.items()
    ]
    stmt = insert(m.Language).values(rows)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[m.Language.iso_639_1],
            set_={
                "english_name": func.coalesce(stmt.excluded.english_name, m.Language.english_name),
                "name": func.coalesce(stmt.excluded.name, m.Language.name),
            },
        )
    )


async def _replace_show_rows(
    session: AsyncSession,
    model: Any,
    show_id: int,
    rows: list[dict[str, Any]],
    *,
    upstream_key: Any = None,
) -> None:
    """Make `rows` the whole of this show's rows in `model`.

    Delete-then-insert, for the same reason `upsert_akas` does it under TV Maze:
    these tables hold sets that upstream both adds to and removes from, and most
    of them carry no upstream row id to upsert against. The surrogate ids churn
    on every rewrite, which is harmless because nothing references them — the
    tables `app` points at are show, season and episode, none of which are
    written this way.

    `upstream_key` names the column that identifies a row upstream, for the three
    tables that have one and allow it to be NULL — `video.tmdb_id`,
    `episode_group.tmdb_id` and `show_creator.credit_id`. A NULL there means the
    row is **locally-authored**, and this delete then steps around it. Every
    other table here has no such column and therefore nothing to protect: a row
    in `show_aka` or `image` is only ever something a payload put there. It is
    the same guard `prune_missing_seasons` spells out, and for the same reason —
    `ON CONFLICT (tmdb_id)` shields an upsert from a NULL for free, a `DELETE`
    has no such shield.
    """
    stmt = delete(model).where(model.show_id == show_id)
    if upstream_key is not None:
        stmt = stmt.where(upstream_key.is_not(None))
    await session.execute(stmt)
    for start in range(0, len(rows), _BATCH_SIZE):
        await session.execute(insert(model).values(rows[start : start + _BATCH_SIZE]))


def _company_rows(companies: Iterable[TMDBCompany]) -> list[dict[str, Any]]:
    return [
        {
            "tmdb_id": c.tmdb_id,
            "name": c.name,
            "logo_path": c.logo_path,
            "origin_country": c.origin_country,
        }
        for c in companies
    ]


async def upsert_show(session: AsyncSession, series: TMDBSeries) -> int:
    """Write the series row, returning its **surrogate** id.

    `runtime` is absent because it is derived rather than sent: `refresh_runtime`
    computes it from the mirrored episodes once they are written (audit D4).

    `is_ended` is absent on purpose: it is a generated column, so writing it
    would be rejected, and that is what stops it drifting from `status`.
    """
    ext = series.external_ids
    values: dict[str, Any] = {
        "tmdb_id": series.tmdb_id,
        "name": series.name,
        "original_name": series.original_name,
        "overview": series.overview,
        "tagline": series.tagline,
        "homepage": series.homepage,
        "type": series.type,
        "adult": series.adult,
        "status": series.status,
        "in_production": series.in_production,
        "first_air_date": series.first_air_date,
        "last_air_date": series.last_air_date,
        "original_language": series.original_language,
        "popularity": series.popularity,
        "vote_average": _dec(series.vote_average),
        "vote_count": series.vote_count,
        "poster_path": series.poster_path,
        "backdrop_path": series.backdrop_path,
        "number_of_episodes": series.number_of_episodes,
        "number_of_seasons": series.number_of_seasons,
        "episode_run_time": series.episode_run_time,
    }
    if ext is not None:
        values |= {
            "imdb_id": ext.imdb_id,
            "tvdb_id": ext.tvdb_id,
            "tvrage_id": ext.tvrage_id,
            "wikidata_id": ext.wikidata_id,
            "freebase_id": ext.freebase_id,
            "freebase_mid": ext.freebase_mid,
            "facebook_id": ext.facebook_id,
            "instagram_id": ext.instagram_id,
            "twitter_id": ext.twitter_id,
        }
    stmt = (
        insert(m.Show)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[m.Show.tmdb_id],
            set_={k: v for k, v in values.items() if k != "tmdb_id"},
        )
        .returning(m.Show.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def upsert_seasons(
    session: AsyncSession, *, show_id: int, series: TMDBSeries
) -> dict[int, int]:
    """Write the show's seasons from `seasons[]`, returning `{tmdb_id: surrogate id}`.

    **Identity comes from `seasons[]`, not from the appended `season/N` block.**
    Measured: the appended form carries `_id` (TMDB-internal) and no `id` at all,
    so it cannot name the season it describes. Only the standalone
    `get_tv_season` response has one. Seasons are therefore created here, and the
    detail blocks are matched to them by `season_number`.
    """
    rows = [
        {
            "tmdb_id": s.tmdb_id,
            "show_id": show_id,
            "season_number": s.season_number,
            "name": s.name,
            "overview": s.overview,
            "poster_path": s.poster_path,
            "air_date": s.air_date,
            "vote_average": _dec(s.vote_average),
            "episode_count": s.episode_count,
        }
        for s in series.seasons
    ]
    return await _upsert_by_tmdb_id(session, m.Season, rows)


async def prune_missing_seasons(
    session: AsyncSession, *, show_id: int, payload_season_ids: set[int]
) -> int:
    """Delete this show's seasons the payload does not name. Returns the count.

    The series payload is authoritative for the show's season set (ADR-0004),
    which is what stops the mirror accruing seasons TMDB created and later
    removed. Two things differ from the TV Maze original.

    **The diff is by `tmdb_id`, not by the primary key**, because the primary key
    is ours and upstream has never heard of it — the same reason every conflict
    target in this module moved.

    **Locally-authored seasons are exempt, and the guard is explicit.** Every
    other write here is protected from them by `ON CONFLICT (tmdb_id)` never
    matching a NULL; a `DELETE` has no such shield, and without the predicate an
    authoritative payload would take a season no upstream feed can restore.

    An empty `payload_season_ids` under an opted-in caller is an authoritative
    zero and deletes every upstream-sourced season, which is why the opt-in
    belongs to the caller — see `upsert_series_payload`.
    """
    stmt = delete(m.Season).where(m.Season.show_id == show_id, m.Season.tmdb_id.is_not(None))
    if payload_season_ids:
        stmt = stmt.where(m.Season.tmdb_id.not_in(payload_season_ids))
    result = await session.execute(stmt)
    pruned: int = result.rowcount  # type: ignore[attr-defined]
    if pruned:
        # Rare by construction, so this is signal rather than noise: it is the
        # only record of the cleanup happening at all.
        log.info("show %d: pruned %d season(s) deleted upstream", show_id, pruned)
    return pruned


async def upsert_episodes(
    session: AsyncSession,
    *,
    show_id: int,
    episodes: Sequence[TMDBEpisode],
    screened_episode_ids: set[int] | None = None,
) -> None:
    """Write a show's episodes, resolving each one's season by number.

    The `{season_number: surrogate id}` map is built from a **live query**, so
    this has to run after the prune: a season about to be deleted must not win
    the lookup and leave its episodes pointed at a row that is going away, only
    for `ON DELETE SET NULL` to blank them. Run in the documented order, a
    duplicate-numbered phantom's episodes land on the survivor instead.

    `screened_episode_ids` is the `screened_theatrically` namespace, or `None`
    when it was not requested — in which case the column is left off the write
    entirely rather than defaulted to false, so a fetch that did not ask cannot
    clear a flag a fetch that did ask set.
    """
    if not episodes:
        return

    season_rows = (
        await session.execute(
            select(m.Season.season_number, m.Season.id)
            .where(m.Season.show_id == show_id)
            .order_by(m.Season.id)
        )
    ).all()
    # Ordered so a show carrying two seasons with one number resolves the same
    # way on every run. TMDB was measured not to do that (0 of 754 shows), but
    # `catalog.season` deliberately carries no unique key on the pair, so the
    # ambiguity is representable and worth being deterministic about.
    season_by_number = {r.season_number: r.id for r in season_rows}

    # The same episode can arrive twice — an appended `season/N` and a
    # `get_tv_season` overflow for the same season, say. Postgres refuses to let
    # one statement's ON CONFLICT touch a row twice, so this is a hard error and
    # not a tidiness concern.
    deduped: dict[int, TMDBEpisode] = {ep.tmdb_id: ep for ep in episodes}

    values_list: list[dict[str, Any]] = []
    for ep in deduped.values():
        row: dict[str, Any] = {
            "tmdb_id": ep.tmdb_id,
            "show_id": show_id,
            "season_id": season_by_number.get(ep.season_number),
            "season_number": ep.season_number,
            "episode_number": ep.episode_number,
            "name": ep.name,
            "overview": ep.overview,
            "air_date": ep.air_date,
            "runtime": ep.runtime,
            "still_path": ep.still_path,
            "production_code": ep.production_code,
            "episode_type": ep.episode_type,
            "vote_average": _dec(ep.vote_average),
            "vote_count": ep.vote_count,
        }
        if screened_episode_ids is not None:
            row["screened_theatrically"] = ep.tmdb_id in screened_episode_ids
        values_list.append(row)

    for start in range(0, len(values_list), _BATCH_SIZE):
        chunk = values_list[start : start + _BATCH_SIZE]
        stmt = insert(m.Episode).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[m.Episode.tmdb_id],
            set_={c: getattr(stmt.excluded, c) for c in chunk[0] if c != "tmdb_id"},
        )
        await session.execute(stmt)


async def _set_air_pointers(session: AsyncSession, *, show_id: int, series: TMDBSeries) -> None:
    """Point `last_episode_to_air` / `next_episode_to_air` at mirrored episodes.

    Resolved after the episodes are written, because they are FKs to the
    surrogate ids those writes mint. An episode TMDB names but we do not mirror
    leaves the pointer null: these are a cache of TMDB's own answer, and a stale
    pointer is worse than none.

    Only fields the payload actually carried are written — `model_fields_set`
    rather than a null check, because `"next_episode_to_air": null` is TMDB
    saying there is no next episode, which is a fact worth storing, while an
    absent key is a payload that never spoke to the question.
    """
    values: dict[str, Any] = {}
    for field, column in (
        ("last_episode_to_air", "last_episode_to_air_id"),
        ("next_episode_to_air", "next_episode_to_air_id"),
    ):
        if field not in series.model_fields_set:
            continue
        episode: TMDBEpisode | None = getattr(series, field)
        values[column] = (
            None
            if episode is None
            else (
                await session.execute(
                    select(m.Episode.id).where(m.Episode.tmdb_id == episode.tmdb_id)
                )
            ).scalar_one_or_none()
        )
    if values:
        await session.execute(update(m.Show).where(m.Show.id == show_id).values(**values))


async def _write_lookup_joins(
    session: AsyncSession, *, show_id: int, series: TMDBSeries, details: list[TMDBSeasonDetail]
) -> dict[int, int]:
    """Genres, networks, companies and keywords, plus the join rows to them.

    Returns the network id map, which `episode_groups` and the season-network
    join both need. Networks are collected across the series *and* every season
    detail in one upsert: a season names its own networks, and interning them
    twice would be two round trips for the same rows.
    """
    genre_ids = await _upsert_by_tmdb_id(
        session, m.Genre, [{"tmdb_id": g.tmdb_id, "name": g.name} for g in series.genres]
    )
    await _replace_show_rows(
        session,
        m.ShowGenre,
        show_id,
        [{"show_id": show_id, "genre_id": gid} for gid in dict.fromkeys(genre_ids.values())],
    )

    network_ids = await _upsert_by_tmdb_id(
        session,
        m.Network,
        _company_rows(
            [*series.networks, *(n for d in details for n in d.networks), *_group_networks(series)]
        ),
    )
    await _replace_show_rows(
        session,
        m.ShowNetwork,
        show_id,
        [
            {"show_id": show_id, "network_id": network_ids[n.tmdb_id]}
            for n in _dedupe_companies(series.networks)
        ],
    )

    company_ids = await _upsert_by_tmdb_id(
        session, m.ProductionCompany, _company_rows(series.production_companies)
    )
    await _replace_show_rows(
        session,
        m.ShowProductionCompany,
        show_id,
        [
            {"show_id": show_id, "company_id": company_ids[c.tmdb_id]}
            for c in _dedupe_companies(series.production_companies)
        ],
    )

    if series.keywords is not None:
        keyword_ids = await _upsert_by_tmdb_id(
            session,
            m.Keyword,
            [{"tmdb_id": k.tmdb_id, "name": k.name} for k in series.keywords.results],
        )
        await _replace_show_rows(
            session,
            m.ShowKeyword,
            show_id,
            [
                {"show_id": show_id, "keyword_id": kid}
                for kid in dict.fromkeys(keyword_ids.values())
            ],
        )
    return network_ids


def _group_networks(series: TMDBSeries) -> list[TMDBCompany]:
    if series.episode_groups is None:
        return []
    return [g.network for g in series.episode_groups.results if g.network is not None]


def _dedupe_companies(companies: Sequence[TMDBCompany]) -> list[TMDBCompany]:
    return list({c.tmdb_id: c for c in companies}.values())


def _dedupe_by_credit_id(creators: Sequence[TMDBCreatedBy]) -> list[TMDBCreatedBy]:
    """First entry wins per `credit_id`; entries without one are all kept."""
    seen: set[str] = set()
    kept: list[TMDBCreatedBy] = []
    for creator in creators:
        if creator.credit_id is not None:
            if creator.credit_id in seen:
                continue
            seen.add(creator.credit_id)
        kept.append(creator)
    return kept


async def _write_countries_and_languages(
    session: AsyncSession, *, show_id: int, series: TMDBSeries
) -> None:
    """The two vocabularies with natural keys, and the four joins into them.

    Written before anything that references them, since `show_origin_country`,
    `show_production_country`, `content_rating` and `show_watch_provider` all
    carry a real FK to `country`.

    All three language concepts are kept apart (audit D3): `original_language` is
    a scalar on the show, `languages[]` and `spoken_languages[]` are their own
    joins, and Breaking Bad genuinely differs across them.
    """
    countries: dict[str, str | None] = {code: None for code in series.origin_country}
    for pc in series.production_countries:
        countries[pc.iso_3166_1] = pc.name or countries.get(pc.iso_3166_1)
    if series.content_ratings is not None:
        for cr in series.content_ratings.results:
            countries.setdefault(cr.iso_3166_1, None)
    if series.watch_providers is not None:
        for code in series.watch_providers.results:
            countries.setdefault(code, None)
    await _ensure_countries(session, countries)

    languages: dict[str, tuple[str | None, str | None]] = {
        code: (None, None) for code in series.languages
    }
    for sl in series.spoken_languages:
        languages[sl.iso_639_1] = (sl.english_name, sl.name)
    await _ensure_languages(session, languages)

    await _replace_show_rows(
        session,
        m.ShowOriginCountry,
        show_id,
        [{"show_id": show_id, "country_code": c} for c in dict.fromkeys(series.origin_country)],
    )
    await _replace_show_rows(
        session,
        m.ShowProductionCountry,
        show_id,
        [
            {"show_id": show_id, "country_code": c}
            for c in dict.fromkeys(pc.iso_3166_1 for pc in series.production_countries)
        ],
    )
    await _replace_show_rows(
        session,
        m.ShowLanguage,
        show_id,
        [{"show_id": show_id, "language_code": c} for c in dict.fromkeys(series.languages)],
    )
    await _replace_show_rows(
        session,
        m.ShowSpokenLanguage,
        show_id,
        [
            {"show_id": show_id, "language_code": c}
            for c in dict.fromkeys(sl.iso_639_1 for sl in series.spoken_languages)
        ],
    )


async def _write_namespaces(
    session: AsyncSession, *, show_id: int, series: TMDBSeries, network_ids: dict[int, int]
) -> None:
    """The appended namespaces that land in show-scoped tables.

    Each one is skipped entirely when its namespace is `None`. `created_by` is
    not a namespace — it rides the series body — so it is always authoritative.
    """
    await _replace_show_rows(
        session,
        m.ShowCreator,
        show_id,
        [
            {
                "show_id": show_id,
                "tmdb_person_id": c.tmdb_person_id,
                "credit_id": c.credit_id,
                "name": c.name,
                "original_name": c.original_name,
                "gender": c.gender,
                "profile_path": c.profile_path,
            }
            # Deduped on `credit_id`, which `uq_show_creator_show_credit` makes
            # unique per show: upstream sending one credit twice would otherwise
            # abort the whole show on an integrity error. A creator with no
            # credit id conflicts with nothing (NULLS DISTINCT) and is kept.
            for c in _dedupe_by_credit_id(series.created_by)
        ],
        upstream_key=m.ShowCreator.credit_id,
    )

    if series.alternative_titles is not None:
        await _replace_show_rows(
            session,
            m.ShowAka,
            show_id,
            [
                {
                    "show_id": show_id,
                    "title": a.title,
                    "country_code": a.iso_3166_1,
                    "type": a.type,
                }
                for a in series.alternative_titles.results
            ],
        )

    if series.content_ratings is not None:
        await _replace_show_rows(
            session,
            m.ContentRating,
            show_id,
            [
                {
                    "show_id": show_id,
                    "country_code": code,
                    "rating": rating.rating,
                    "descriptors": rating.descriptors,
                }
                # One row per country, which the table's unique key requires and
                # which upstream has been known to violate.
                for code, rating in {
                    r.iso_3166_1: r for r in series.content_ratings.results
                }.items()
            ],
        )

    if series.translations is not None:
        await _replace_show_rows(
            session,
            m.Translation,
            show_id,
            [
                {
                    "show_id": show_id,
                    "language_code": t.iso_639_1,
                    "country_code": t.iso_3166_1,
                    "language_name": t.name,
                    "language_english_name": t.english_name,
                    "name": t.data.name,
                    "overview": t.data.overview,
                    "tagline": t.data.tagline,
                    "homepage": t.data.homepage,
                }
                for t in {
                    (t.iso_639_1, t.iso_3166_1): t for t in series.translations.translations
                }.values()
            ],
        )

    if series.images is not None:
        await _replace_show_rows(
            session,
            m.Image,
            show_id,
            [
                {
                    "show_id": show_id,
                    "kind": kind,
                    "file_path": image.file_path,
                    "aspect_ratio": image.aspect_ratio,
                    "height": image.height,
                    "width": image.width,
                    "language_code": image.iso_639_1,
                    "vote_average": _dec(image.vote_average),
                    "vote_count": image.vote_count,
                }
                for kind, images in (
                    ("backdrop", series.images.backdrops),
                    ("logo", series.images.logos),
                    ("poster", series.images.posters),
                )
                for image in {i.file_path: i for i in images}.values()
            ],
        )

    if series.videos is not None:
        await _replace_show_rows(
            session,
            m.Video,
            show_id,
            [
                {
                    "show_id": show_id,
                    "tmdb_id": v.tmdb_id,
                    "key": v.key,
                    "name": v.name,
                    "site": v.site,
                    "size": v.size,
                    "type": v.type,
                    "official": v.official,
                    "published_at": v.published_at,
                    "language_code": v.iso_639_1,
                    "country_code": v.iso_3166_1,
                }
                for v in {v.tmdb_id: v for v in series.videos.results}.values()
            ],
            upstream_key=m.Video.tmdb_id,
        )

    if series.episode_groups is not None:
        await _replace_show_rows(
            session,
            m.EpisodeGroup,
            show_id,
            [
                {
                    "show_id": show_id,
                    "tmdb_id": g.tmdb_id,
                    "name": g.name,
                    "description": g.description,
                    "episode_count": g.episode_count,
                    "group_count": g.group_count,
                    "type": g.type,
                    "network_id": None if g.network is None else network_ids[g.network.tmdb_id],
                }
                for g in {g.tmdb_id: g for g in series.episode_groups.results}.values()
            ],
            upstream_key=m.EpisodeGroup.tmdb_id,
        )

    if series.watch_providers is not None:
        await _write_watch_providers(session, show_id=show_id, providers=series.watch_providers)


async def _write_watch_providers(
    session: AsyncSession, *, show_id: int, providers: TMDBWatchProviders
) -> None:
    """Where a show can be watched, per country and per kind of offer.

    Storing this carries no obligation; **displaying it obliges us to attribute
    JustWatch on each media item**, in addition to TMDB, and non-compliance is
    explicit grounds for revoking API access. See `catalog.models.WatchProvider`.
    """
    by_country = providers.results
    provider_ids = await _upsert_by_tmdb_id(
        session,
        m.WatchProvider,
        [
            {
                "tmdb_id": offer.provider_id,
                "name": offer.provider_name,
                "logo_path": offer.logo_path,
                # Best effort: TMDB states a global priority on
                # `/watch/providers/tv`, and what rides here is the priority
                # within one country. Close enough to order a picker by, and the
                # per-item value is kept on the join row regardless.
                "display_priority": offer.display_priority,
            }
            for country in by_country.values()
            for _, offer in country.offers()
        ],
    )

    rows: dict[tuple[int, str, str, int], dict[str, Any]] = {}
    for code, country in by_country.items():
        for offer_type, offer in country.offers():
            provider_id = provider_ids[offer.provider_id]
            rows[(show_id, code, offer_type, provider_id)] = {
                "show_id": show_id,
                "country_code": code,
                "offer_type": offer_type,
                "provider_id": provider_id,
                "display_priority": offer.display_priority,
                # Denormalised across a country's offer types because that is
                # the grain TMDB gives it at, and the grain a UI needs it at.
                "link": country.link,
            }
    await _replace_show_rows(session, m.ShowWatchProvider, show_id, list(rows.values()))


async def _write_season_networks(
    session: AsyncSession,
    *,
    details: list[TMDBSeasonDetail],
    season_ids: dict[int, int],
    number_to_tmdb: dict[int, int],
    network_ids: dict[int, int],
) -> None:
    """`networks` at season grain — measured on the season payload, and absent
    from the ticket's inventory (audit §8)."""
    scoped = [
        season_ids[number_to_tmdb[d.season_number]]
        for d in details
        if d.season_number in number_to_tmdb
    ]
    if not scoped:
        return
    await session.execute(delete(m.SeasonNetwork).where(m.SeasonNetwork.season_id.in_(scoped)))
    rows: dict[tuple[int, int], dict[str, Any]] = {}
    for detail in details:
        tmdb_id = number_to_tmdb.get(detail.season_number)
        if tmdb_id is None:
            continue
        season_id = season_ids[tmdb_id]
        for network in detail.networks:
            rows[(season_id, network_ids[network.tmdb_id])] = {
                "season_id": season_id,
                "network_id": network_ids[network.tmdb_id],
            }
    if rows:
        await session.execute(insert(m.SeasonNetwork).values(list(rows.values())))


async def upsert_series_payload(
    session: AsyncSession,
    series: TMDBSeries,
    *,
    seasons: Sequence[TMDBSeasonDetail] | None = None,
    prune_seasons: bool = False,
) -> int:
    """Write a complete TMDB payload into `catalog`, returning the surrogate show id.

    `seasons` is the `get_tv_season` overflow — the seasons that did not fit in
    the 20-entry `append_to_response` budget. It is merged with the appended
    blocks by `season_number`, with the explicitly-passed detail winning, so a
    caller re-fetching one season can hand it over without rebuilding the rest.

    `prune_seasons` makes the payload authoritative for the show's season set and
    is **opt-in per caller, which it must stay**. `TMDBSeries.seasons` defaults
    to `[]`, so this function cannot tell "no seasons upstream" from "the caller
    fetched something narrower" — the same reason `tvmaze.upsert` refuses the
    tempting `if not seasons: skip` guard, which additionally conflates a
    legitimately season-less show with a missing one.

    Order is load-bearing in three places:

    1. The show is written first, because everything else FKs to it.
    2. The prune runs **between** the season write and the episode write, so the
       season lookup episodes resolve through holds only survivors.
    3. The air pointers and the derived `runtime` are set **after** the episodes
       — the pointers because they are FKs to surrogate ids those writes mint,
       the runtime because it is a median over the mirrored episodes rather than
       over whatever this one call happened to carry.

    The caller owns the transaction.
    """
    details_by_number: dict[int, TMDBSeasonDetail] = {
        d.season_number: d for d in series.appended_seasons
    }
    for detail in seasons or ():
        details_by_number[detail.season_number] = detail
    details = list(details_by_number.values())
    episodes = [ep for detail in details for ep in detail.episodes]

    show_id = await upsert_show(session, series)

    await _write_countries_and_languages(session, show_id=show_id, series=series)
    network_ids = await _write_lookup_joins(
        session, show_id=show_id, series=series, details=details
    )
    await _write_namespaces(session, show_id=show_id, series=series, network_ids=network_ids)

    season_ids = await upsert_seasons(session, show_id=show_id, series=series)
    if prune_seasons:
        await prune_missing_seasons(session, show_id=show_id, payload_season_ids=set(season_ids))

    number_to_tmdb = {s.season_number: s.tmdb_id for s in series.seasons}
    await _write_season_networks(
        session,
        details=details,
        season_ids=season_ids,
        number_to_tmdb=number_to_tmdb,
        network_ids=network_ids,
    )

    screened: set[int] | None = None
    if series.screened_theatrically is not None:
        screened = {e.tmdb_id for e in series.screened_theatrically.results}
    await upsert_episodes(
        session, show_id=show_id, episodes=episodes, screened_episode_ids=screened
    )
    await _set_air_pointers(session, show_id=show_id, series=series)
    await refresh_runtime(session, show_id=show_id)
    return show_id
