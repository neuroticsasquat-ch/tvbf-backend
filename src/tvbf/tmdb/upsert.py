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

**Show credits are one entry to many rows; episode credits are one to one.**
`aggregate_credits` nests `roles[]` / `jobs[]` under a person, so one cast entry
becomes one `show_cast` row *per character* and one crew entry one `show_crew`
row *per job*. Episode `guest_stars` / `crew` ride the season payload and do not
nest — an appearance is a row (NEU-1040). Character and crew role are interned
rather than upserted on an upstream id in both cases, because neither is an
entity upstream; character interns against the **show** at either grain, so a
guest star resolves to the character the show cast already named. See
`_intern_characters` and `_write_episode_credits`.
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
    TMDBAggregateCredits,
    TMDBAggregateCrew,
    TMDBCompany,
    TMDBCreatedBy,
    TMDBCreditPerson,
    TMDBEpisode,
    TMDBEpisodeCrewMember,
    TMDBJob,
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

    Batched for the same reason `upsert_episodes` is, and it stopped being
    theoretical with `person` (NEU-1039): the lookup tables here hold a few dozen
    rows per show, but a show's credited people run into the thousands — The
    Simpsons alone has 1,420 cast and 533 crew. A person binds 8 columns, so the
    32,767-parameter cap falls at **4,095 credited people** on one show, which
    the long-running sketch and variety formats reach and nothing else in this
    module comes close to.

    Rows must share a key set — the update clause is built from the first.
    """
    if not rows:
        return {}
    # The same network can appear on the series and on several of its seasons;
    # the same person on several credits of one show.
    deduped = {r["tmdb_id"]: r for r in rows}
    values = list(deduped.values())
    ids: dict[Any, int] = {}
    for start in range(0, len(values), _BATCH_SIZE):
        chunk = values[start : start + _BATCH_SIZE]
        stmt = insert(model).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[model.tmdb_id],
            set_={c: getattr(stmt.excluded, c) for c in chunk[0] if c != "tmdb_id"},
        ).returning(model.tmdb_id, model.id)
        ids |= {row.tmdb_id: row.id for row in (await session.execute(stmt)).all()}
    return ids


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


def _dedupe_episodes(episodes: Sequence[TMDBEpisode]) -> dict[int, TMDBEpisode]:
    """`{tmdb_id: episode}`, last occurrence winning.

    The same episode can arrive twice — an appended `season/N` and a
    `get_tv_season` overflow for the same season, say. Postgres refuses to let
    one statement's `ON CONFLICT` touch a row twice, so this is a hard error and
    not a tidiness concern.

    Shared by the episode write and the credit write so both spend the *same*
    payload for a duplicated episode. Deduplicating twice, independently, would
    let the row come from one copy and its guest cast from the other.
    """
    return {ep.tmdb_id: ep for ep in episodes}


async def upsert_episodes(
    session: AsyncSession,
    *,
    show_id: int,
    episodes: Sequence[TMDBEpisode],
    screened_episode_ids: set[int] | None = None,
) -> dict[int, int]:
    """Write a show's episodes, returning `{tmdb_id: surrogate id}`.

    Each episode's season is resolved by number. The `{season_number: surrogate
    id}` map is built from a **live query**, so this has to run after the prune:
    a season about to be deleted must not win the lookup and leave its episodes
    pointed at a row that is going away, only for `ON DELETE SET NULL` to blank
    them. Run in the documented order, a duplicate-numbered phantom's episodes
    land on the survivor instead.

    `screened_episode_ids` is the `screened_theatrically` namespace, or `None`
    when it was not requested — in which case the column is left off the write
    entirely rather than defaulted to false, so a fetch that did not ask cannot
    clear a flag a fetch that did ask set.

    The returned map is what episode-grain credits are written against
    (NEU-1040): `episode_guest_cast` and `episode_crew` reference the surrogate,
    and this statement is the only place it is known without a re-query.
    """
    if not episodes:
        return {}

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

    deduped = _dedupe_episodes(episodes)

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

    episode_ids: dict[int, int] = {}
    for start in range(0, len(values_list), _BATCH_SIZE):
        chunk = values_list[start : start + _BATCH_SIZE]
        stmt = insert(m.Episode).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[m.Episode.tmdb_id],
            set_={c: getattr(stmt.excluded, c) for c in chunk[0] if c != "tmdb_id"},
        ).returning(m.Episode.tmdb_id, m.Episode.id)
        episode_ids |= {row.tmdb_id: row.id for row in (await session.execute(stmt)).all()}
    return episode_ids


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

    if series.aggregate_credits is not None:
        await _write_credits(session, show_id=show_id, credits=series.aggregate_credits)


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


async def _intern_characters(
    session: AsyncSession, *, show_id: int, names: Sequence[str]
) -> dict[str, int]:
    """Intern this show's character names, returning `{name: surrogate id}`.

    Interning rather than upserting, because a character is not an entity
    upstream: TMDB sends free text on a credit, so there is no `tmdb_id` to
    conflict on and the natural key `(show_id, name)` is all there is. Narrowing
    the scope to one show is NEU-1038's one real model change — see
    `catalog.models.Character`.

    `DO UPDATE` rather than `DO NOTHING` even though the update is a no-op:
    `RETURNING` reports only the rows a statement touched, and a `DO NOTHING`
    would silently omit every character this show already had — which, on the
    second pass over any show, is all of them.
    """
    if not names:
        return {}
    ids: dict[str, int] = {}
    unique = list(dict.fromkeys(names))
    for start in range(0, len(unique), _BATCH_SIZE):
        chunk = [{"show_id": show_id, "name": name} for name in unique[start : start + _BATCH_SIZE]]
        stmt = insert(m.Character).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[m.Character.show_id, m.Character.name],
            set_={"name": stmt.excluded.name},
        ).returning(m.Character.name, m.Character.id)
        ids |= {row.name: row.id for row in (await session.execute(stmt)).all()}
    return ids


async def _intern_crew_roles(
    session: AsyncSession, pairs: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], int]:
    """Intern `(department, job)` pairs, returning `{pair: surrogate id}`.

    One vocabulary shared by show crew and episode crew, unlike `tvmaze`'s two —
    measured 100% overlap, see `catalog.models.CrewRole`. So this is deliberately
    not scoped to a show, unlike `_intern_characters`: a re-run for one show
    reuses the pairs every other show has already interned.

    `DO UPDATE` over a no-op rather than `DO NOTHING`, for the reason
    `_intern_characters` spells out — `RETURNING` reports only the rows a
    statement touched, and here that would be almost none, since a mature
    vocabulary is already interned by the time any given show is written.
    """
    if not pairs:
        return {}
    ids: dict[tuple[str, str], int] = {}
    unique = list(dict.fromkeys(pairs))
    for start in range(0, len(unique), _BATCH_SIZE):
        chunk = [
            {"department": department, "job": job}
            for department, job in unique[start : start + _BATCH_SIZE]
        ]
        stmt = insert(m.CrewRole).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[m.CrewRole.department, m.CrewRole.job],
            set_={"job": stmt.excluded.job},
        ).returning(m.CrewRole.department, m.CrewRole.job, m.CrewRole.id)
        ids |= {(row.department, row.job): row.id for row in (await session.execute(stmt)).all()}
    return ids


def _person_row(credit: TMDBCreditPerson) -> dict[str, Any]:
    """The `catalog.person` columns a credit carries. Cast and crew entries are
    identical here — the difference between them is what nests underneath."""
    return {
        "tmdb_id": credit.tmdb_person_id,
        "name": credit.name,
        "original_name": credit.original_name,
        "gender": credit.gender,
        "known_for_department": credit.known_for_department,
        "popularity": credit.popularity,
        "profile_path": credit.profile_path,
        "adult": credit.adult,
    }


def _character_name(character: str | None) -> str | None:
    """A credit's character name, or `None` when upstream sent nothing usable.

    Blank is rare and real — 1 of 7,629 sampled show-level roles, and 0 of 1,460
    sampled episode guest stars. `catalog.show_cast` and
    `catalog.episode_guest_cast` both allow a null `character_id` for exactly
    this, because interning `''` would invent a character nobody played and NOT
    NULL would abort a multi-hour pass on one row.

    Takes the raw string rather than a credit, because the two grains carry it on
    differently-shaped entries — nested in `roles[]` at show level, on the entry
    itself for a guest star — and the answer must not differ between them.
    """
    name = (character or "").strip()
    return name or None


def _crew_credits(
    crew: Sequence[TMDBAggregateCrew], *, show_id: int
) -> list[tuple[TMDBAggregateCrew, TMDBJob, tuple[str, str]]]:
    """Flatten crew entries to `(member, job, (department, job))` triples.

    One place rather than two, because the interning pass and the row build have
    to agree on exactly which jobs survive: the writer looks each row's pair up
    in the map the interning pass produced, so a guard that drifted between them
    would be a `KeyError` thousands of shows into a multi-hour run.

    `crew_role` is NOT NULL in both columns, so a pair missing either half cannot
    be interned. Measured never to happen — which is why it is logged rather than
    raised: one malformed entry must not cost the show its whole payload, and a
    warning is how we would learn the measurement had gone stale.
    """
    triples: list[tuple[TMDBAggregateCrew, TMDBJob, tuple[str, str]]] = []
    for member in crew:
        for job in member.jobs:
            if not member.department or not job.job:
                log.warning(
                    "show %d: skipped crew credit for person %d — department=%r job=%r",
                    show_id,
                    member.tmdb_person_id,
                    member.department,
                    job.job,
                )
                continue
            triples.append((member, job, (member.department, job.job)))
    return triples


async def _write_credits(
    session: AsyncSession, *, show_id: int, credits: TMDBAggregateCredits
) -> None:
    """Show-level cast and crew from `aggregate_credits` (NEU-1039).

    **One row per credit, not per person.** `aggregate_credits` nests
    `roles: [{credit_id, character, episode_count}]` under a cast entry, so an
    actor who played two characters on one show becomes two rows sharing a
    `person_id` — which is what makes `episode_count` a per-character measure
    rather than the billing-order proxy TV Maze gave us. Crew nests `jobs[]` the
    same way.

    **Delete-then-insert, with no locally-authored exemption**, unlike
    `show_creator`. `credit_id` is nullable on both tables, but a null one here
    means upstream omitted it rather than that somebody authored the credit by
    hand — so exempting those rows from the delete would let a copy accumulate on
    every pass. The tables carry no unique key to catch that, which is why the
    refresh has to be total (see `catalog.models.ShowCast`).

    People are interned across shows on `tmdb_id`; characters within this show on
    their name; crew roles across TMDB on `(department, job)`.

    **Nothing here prunes `catalog.character`**, and that is a decision rather
    than an omission. A character whose last cast credit disappears leaves its
    row behind, which is the cheaper of two wrongs: the same rows are the
    interning target for episode guest cast (NEU-1040), which is written from a
    different payload, so a prune scoped to this one would delete characters an
    episode credit still references. The residue is bounded by a show's own
    upstream renames, and `character` carries nothing but a name.
    """
    person_ids = await _upsert_by_tmdb_id(
        session, m.Person, [_person_row(c) for c in (*credits.cast, *credits.crew)]
    )
    character_ids = await _intern_characters(
        session,
        show_id=show_id,
        names=[
            name
            for member in credits.cast
            for portrayal in member.roles
            if (name := _character_name(portrayal.character)) is not None
        ],
    )
    crew_credits = _crew_credits(credits.crew, show_id=show_id)
    crew_role_ids = await _intern_crew_roles(session, [pair for _, _, pair in crew_credits])

    await _replace_show_rows(
        session,
        m.ShowCast,
        show_id,
        [
            {
                "show_id": show_id,
                "person_id": person_ids[member.tmdb_person_id],
                "character_id": (
                    None
                    if (name := _character_name(portrayal.character)) is None
                    else character_ids[name]
                ),
                "credit_id": portrayal.credit_id,
                "episode_count": portrayal.episode_count,
                "total_episode_count": member.total_episode_count,
                "billing_order": member.billing_order,
            }
            for member in credits.cast
            for portrayal in member.roles
        ],
    )
    await _replace_show_rows(
        session,
        m.ShowCrew,
        show_id,
        [
            {
                "show_id": show_id,
                "person_id": person_ids[member.tmdb_person_id],
                "role_id": crew_role_ids[pair],
                "credit_id": job.credit_id,
                "episode_count": job.episode_count,
                "total_episode_count": member.total_episode_count,
            }
            for member, job, pair in crew_credits
        ],
    )


async def _replace_episode_rows(
    session: AsyncSession, model: Any, episode_ids: Sequence[int], rows: list[dict[str, Any]]
) -> None:
    """Make `rows` the whole of these episodes' rows in `model`.

    The episode-grain twin of `_replace_show_rows`, scoped to the episodes whose
    payload actually carried the credit list rather than to the show. That scope
    is the point: a delta or a single-season re-fetch speaks for the episodes it
    returned and for no others, and a show-wide delete would empty the credits of
    every season it did not carry.

    No locally-authored exemption, for the same reason `_write_credits` needs
    none: a null `credit_id` on these tables means upstream omitted it, not that
    somebody authored the credit by hand.

    Both halves are batched against the 32,767-parameter cap — the delete
    because a soap binds one parameter per episode and can pass 12,000 of them
    in a single pass.
    """
    for start in range(0, len(episode_ids), _BATCH_SIZE):
        await session.execute(
            delete(model).where(model.episode_id.in_(episode_ids[start : start + _BATCH_SIZE]))
        )
    for start in range(0, len(rows), _BATCH_SIZE):
        await session.execute(insert(model).values(rows[start : start + _BATCH_SIZE]))


def _episode_crew_credits(
    episodes: Sequence[tuple[int, TMDBEpisode]], *, show_id: int
) -> list[tuple[int, TMDBEpisodeCrewMember, tuple[str, str]]]:
    """Flatten to `(episode_id, member, (department, job))` triples.

    The episode-grain twin of `_crew_credits`, and one place rather than two for
    the same reason: the interning pass and the row build have to agree on
    exactly which credits survive, or a pair that drifted between them is a
    `KeyError` thousands of shows into a multi-hour run.

    A pair missing either half cannot be interned, since `crew_role` is NOT NULL
    in both columns. Measured never to happen — 0 blank of 7,456 sampled entries
    (`scripts/probe_tmdb_episode_credits_append.py`) — which is exactly why it is
    logged rather than raised: one malformed entry must not cost the show its
    whole payload, and the warning is how we would learn the measurement had gone
    stale.
    """
    triples: list[tuple[int, TMDBEpisodeCrewMember, tuple[str, str]]] = []
    for episode_id, episode in episodes:
        for member in episode.crew or ():
            if not member.department or not member.job:
                log.warning(
                    "show %d episode %d: skipped crew credit for person %d — department=%r job=%r",
                    show_id,
                    episode_id,
                    member.tmdb_person_id,
                    member.department,
                    member.job,
                )
                continue
            triples.append((episode_id, member, (member.department, member.job)))
    return triples


async def _write_episode_credits(
    session: AsyncSession,
    *,
    show_id: int,
    episodes: Sequence[TMDBEpisode],
    episode_ids: dict[int, int],
) -> None:
    """Guest cast and crew at episode grain, from the season payload (NEU-1040).

    **This costs no request at all**, which is the finding the ticket turned on.
    TV Maze needed a dedicated ~29-hour pass over 188k seasons for the same data
    (ADR-0003), so the question was whether TMDB's episode credits ride the
    *appended* `season/N` block or only the standalone season fetch the ingest
    does not make. Measured 2026-08-11
    (`scripts/probe_tmdb_episode_credits_append.py`): the appended block carries
    them, at exact parity with the standalone response — same 1,460 guest stars
    and 7,456 crew entries, same key sets, no episode truncated. So there is no
    second pass, no per-season watermark, and nothing to resume beyond the show
    grain `tmdb_synced_at` already covers.

    **Flat, where show credits nest.** A guest star appears in one episode as one
    character and a crew member holds one job, so an entry is a row — there is no
    `roles[]` / `jobs[]` to unpack and no `episode_count`, because the appearance
    *is* the count.

    **Absent is not empty, per episode.** An episode whose payload carried no
    `guest_stars` key is left out of that table's refresh entirely rather than
    having its guest cast cleared — the distinction `TMDBEpisode` draws for
    exactly this, and the two lists are scoped independently because an episode
    can carry one without the other.

    Characters intern against **the show**, not the episode: a guest star's role
    is a role of the show, so a returning guest resolves to the character the
    show cast already interned (see `catalog.models.EpisodeGuestCast`). Crew
    roles intern across TMDB, into the one vocabulary show crew shares.

    Rows are deduplicated on the three-part keys the tables enforce —
    `(episode, person, character)` and `(episode, person, role)`. Upstream
    sending one credit twice is otherwise an integrity error that costs the whole
    show, and Postgres will not let one statement's `ON CONFLICT` touch a row
    twice either way.

    Episodes are run through `_dedupe_episodes` **here** rather than trusted to
    have been deduplicated by the caller: a duplicated episode resolves to one
    surrogate id, so two copies of it would contribute their guest lists twice to
    the same episode and silently merge them.
    """
    scoped = [
        (episode_ids[ep.tmdb_id], ep)
        for ep in _dedupe_episodes(episodes).values()
        if ep.tmdb_id in episode_ids
    ]
    guest_scope = [eid for eid, ep in scoped if ep.guest_stars is not None]
    crew_scope = [eid for eid, ep in scoped if ep.crew is not None]
    if not guest_scope and not crew_scope:
        return

    guests = [(eid, guest) for eid, ep in scoped for guest in ep.guest_stars or ()]
    crew_credits = _episode_crew_credits(scoped, show_id=show_id)

    person_ids = await _upsert_by_tmdb_id(
        session,
        m.Person,
        [_person_row(guest) for _, guest in guests]
        + [_person_row(member) for _, member, _ in crew_credits],
    )
    character_ids = await _intern_characters(
        session,
        show_id=show_id,
        names=[
            name for _, guest in guests if (name := _character_name(guest.character)) is not None
        ],
    )
    crew_role_ids = await _intern_crew_roles(session, [pair for _, _, pair in crew_credits])

    guest_rows: dict[tuple[int, int, int | None], dict[str, Any]] = {}
    for episode_id, guest in guests:
        name = _character_name(guest.character)
        character_id = None if name is None else character_ids[name]
        person_id = person_ids[guest.tmdb_person_id]
        guest_rows.setdefault(
            (episode_id, person_id, character_id),
            {
                "episode_id": episode_id,
                "person_id": person_id,
                "character_id": character_id,
                "credit_id": guest.credit_id,
                "credit_order": guest.credit_order,
            },
        )
    await _replace_episode_rows(session, m.EpisodeGuestCast, guest_scope, list(guest_rows.values()))

    crew_rows: dict[tuple[int, int, int], dict[str, Any]] = {}
    for episode_id, member, pair in crew_credits:
        person_id = person_ids[member.tmdb_person_id]
        role_id = crew_role_ids[pair]
        crew_rows.setdefault(
            (episode_id, person_id, role_id),
            {
                "episode_id": episode_id,
                "person_id": person_id,
                "role_id": role_id,
                "credit_id": member.credit_id,
            },
        )
    await _replace_episode_rows(session, m.EpisodeCrew, crew_scope, list(crew_rows.values()))


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
    episode_ids = await upsert_episodes(
        session, show_id=show_id, episodes=episodes, screened_episode_ids=screened
    )
    await _write_episode_credits(
        session, show_id=show_id, episodes=episodes, episode_ids=episode_ids
    )
    await _set_air_pointers(session, show_id=show_id, series=series)
    await refresh_runtime(session, show_id=show_id)
    return show_id
