from collections.abc import Mapping
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import NotFound
from tvbf.app.repos import episode_repo, episode_watch_repo, show_membership_repo, show_repo
from tvbf.app.schemas import (
    MyShowEntry,
    MyShowsSort,
    UpcomingEntry,
    UpcomingSort,
    WatchNextEntry,
    WatchNextSort,
)
from tvbf.sorting import show_name_sort_key
from tvbf.tvmaze.browse_queries import hydrate_show_refs
from tvbf.tvmaze.models import Show
from tvbf.tvmaze.schemas import EpisodeOut, NetworkRef, ShowSummary, build_show_summary


def _episode_to_out(ep: object, *, watched: bool | None = None) -> EpisodeOut:
    out = EpisodeOut.model_validate(ep, from_attributes=True)
    if watched is not None:
        out = out.model_copy(update={"watched": watched})
    return out


def build_show_summary_from_refs(
    show: Show,
    *,
    genres_by_show: Mapping[int, list[str]],
    networks_by_id: Mapping[int, object],
    wcs_by_id: Mapping[int, object],
) -> ShowSummary:
    """Resolve genre/network/web-channel refs against pre-loaded lookup maps and
    build a ShowSummary. Pure: no DB calls, no side effects."""
    genre_names = genres_by_show.get(show.id, [])
    network_row = networks_by_id.get(show.network_id) if show.network_id else None
    wc_row = wcs_by_id.get(show.web_channel_id) if show.web_channel_id else None
    network_ref = (
        NetworkRef(id=network_row.id, name=network_row.name)  # type: ignore[attr-defined]
        if network_row
        else None
    )
    wc_ref = (
        NetworkRef(id=wc_row.id, name=wc_row.name)  # type: ignore[attr-defined]
        if wc_row
        else None
    )
    return build_show_summary(show, genre_names, network_ref, wc_ref)


# ---------------------------------------------------------------------------
# Pure sort functions — no DB, no I/O. Unit-tested directly.
# ---------------------------------------------------------------------------

_EPOCH = datetime.fromtimestamp(0, tz=UTC).date()
_EPOCH_DT = datetime.fromtimestamp(0, tz=UTC)


def sort_my_shows(
    entries: list[MyShowEntry],
    sort: MyShowsSort,
    latest_aired: Mapping[int, date],
) -> list[MyShowEntry]:
    """Order My Shows entries by the requested sort mode.

    `recent_activity` (default) — by `latest_aired[show.id]` desc, then name asc as
    a tiebreaker. Shows with no aired episodes fall to the bottom (epoch fallback).
    `name_asc` / `name_desc` — case-insensitive by show name.
    `added` — by `added_at` desc.
    """
    if sort == "name_asc":
        return sorted(entries, key=lambda e: show_name_sort_key(e.show.name))
    if sort == "name_desc":
        return sorted(entries, key=lambda e: show_name_sort_key(e.show.name), reverse=True)
    if sort == "added":
        return sorted(entries, key=lambda e: e.added_at, reverse=True)
    # recent_activity (default)
    return sorted(
        entries,
        key=lambda e: (latest_aired.get(e.show.id) or _EPOCH, show_name_sort_key(e.show.name)),
        reverse=True,
    )


def sort_watch_next(entries: list[WatchNextEntry], sort: WatchNextSort) -> list[WatchNextEntry]:
    """Order Watch Next entries. Default `airdate_desc` orders by the show's
    most recent aired episode (`entry.last_aired`). `unwatched_airdate_desc`
    orders by the unwatched episode's airdate. Entries with a None date fall
    to the bottom (date.min fallback)."""
    if sort == "airdate_asc":
        return sorted(
            entries, key=lambda e: (e.episode.airdate or date.min, show_name_sort_key(e.show.name))
        )
    if sort == "unwatched_airdate_desc":
        return sorted(
            entries,
            key=lambda e: (e.episode.airdate or date.min, show_name_sort_key(e.show.name)),
            reverse=True,
        )
    if sort == "name_asc":
        return sorted(entries, key=lambda e: show_name_sort_key(e.show.name))
    if sort == "name_desc":
        return sorted(entries, key=lambda e: show_name_sort_key(e.show.name), reverse=True)
    # airdate_desc (default): show's most recent aired episode
    return sorted(
        entries,
        key=lambda e: (e.last_aired or date.min, show_name_sort_key(e.show.name)),
        reverse=True,
    )


def sort_upcoming(entries: list[UpcomingEntry], sort: UpcomingSort) -> list[UpcomingEntry]:
    """Order Upcoming entries. Default `airdate_asc` (soonest first)."""
    if sort == "airdate_desc":
        return sorted(
            entries,
            key=lambda e: (e.episode.airdate or date.min, show_name_sort_key(e.show.name)),
            reverse=True,
        )
    if sort == "added_desc":
        return sorted(
            entries,
            key=lambda e: (e.added_at or _EPOCH_DT, show_name_sort_key(e.show.name)),
            reverse=True,
        )
    if sort == "name_asc":
        return sorted(entries, key=lambda e: show_name_sort_key(e.show.name))
    if sort == "name_desc":
        return sorted(entries, key=lambda e: show_name_sort_key(e.show.name), reverse=True)
    # airdate_asc (default)
    return sorted(
        entries, key=lambda e: (e.episode.airdate or date.min, show_name_sort_key(e.show.name))
    )


async def add(db: AsyncSession, *, user_id: UUID, show_id: int) -> None:
    """Verify show exists, add membership, commit. Raises NotFound if show
    does not exist in the catalog."""
    show = await show_repo.get_by_id(db, show_id)
    if show is None:
        raise NotFound()
    await show_membership_repo.add(db, user_id=user_id, show_id=show_id)
    await db.commit()


async def remove(db: AsyncSession, *, user_id: UUID, show_id: int) -> None:
    """Remove membership row (idempotent), commit."""
    await show_membership_repo.remove(db, user_id=user_id, show_id=show_id)
    await db.commit()


async def list_my_shows(
    db: AsyncSession,
    *,
    user_id: UUID,
    sort: MyShowsSort = "recent_activity",
    today: date | None = None,
) -> list[MyShowEntry]:
    pairs = await show_membership_repo.list_with_added_at(db, user_id)
    if not pairs:
        return []

    shows = [show for show, _added in pairs]
    show_ids = [show.id for show in shows]
    added_at_by_show = {show.id: added for show, added in pairs}

    genres_by_show, networks_by_id, wcs_by_id = await hydrate_show_refs(db, shows)
    total_counts = await episode_repo.count_per_show(db, show_ids)
    watched_counts = await episode_watch_repo.count_watched_per_show(
        db, user_id=user_id, show_ids=show_ids
    )
    today_d = today if today is not None else date.today()
    latest_aired = await episode_repo.latest_aired_per_show(db, show_ids, today_d)
    aired_counts = await episode_repo.count_aired_per_show(db, show_ids, today_d)
    last_watched = await episode_watch_repo.latest_watched_per_show(
        db, user_id=user_id, show_ids=show_ids
    )

    next_eps_by_show: dict[int, object] = {}
    for show in shows:
        next_ep = await episode_repo.next_unwatched(db, user_id=user_id, show_id=show.id)
        if next_ep is not None:
            next_eps_by_show[show.id] = next_ep
    next_ep_ids = {ep.id for ep in next_eps_by_show.values()}  # type: ignore[attr-defined]
    watched_next_ids = await episode_watch_repo.watched_in(
        db, user_id=user_id, episode_ids=next_ep_ids
    )

    entries: list[MyShowEntry] = []
    for show in shows:
        next_ep = next_eps_by_show.get(show.id)
        total = total_counts.get(show.id, 0)
        aired = aired_counts.get(show.id, 0)
        entries.append(
            MyShowEntry(
                show=build_show_summary_from_refs(
                    show,
                    genres_by_show=genres_by_show,
                    networks_by_id=networks_by_id,
                    wcs_by_id=wcs_by_id,
                ),
                watched_episode_count=watched_counts.get(show.id, 0),
                total_episode_count=total,
                aired_episode_count=aired,
                upcoming_episode_count=total - aired,
                last_aired=latest_aired.get(show.id),
                last_watched_at=last_watched.get(show.id),
                next_episode=(
                    _episode_to_out(
                        next_ep,
                        watched=next_ep.id in watched_next_ids,  # type: ignore[attr-defined]
                    )
                    if next_ep is not None
                    else None
                ),
                added_at=added_at_by_show[show.id],
            )
        )

    return sort_my_shows(entries, sort, latest_aired)


async def list_watch_next(
    db: AsyncSession,
    *,
    user_id: UUID,
    sort: WatchNextSort = "airdate_desc",
    today: date | None = None,
) -> list[WatchNextEntry]:
    """Per show in My Shows, the earliest unwatched episode whose airdate has
    already passed. Shows with nothing unwatched-and-aired are omitted."""
    today_d = today if today is not None else date.today()
    episodes = await episode_repo.earliest_aired_unwatched_per_show(
        db, user_id=user_id, today=today_d
    )
    if not episodes:
        return []

    # Load the associated Show for each episode.
    show_ids = list({ep.show_id for ep in episodes})
    shows_by_id: dict[int, Show] = {}
    for sid in show_ids:
        show = await show_repo.get_by_id(db, sid)
        if show is not None:
            shows_by_id[sid] = show
    added_at_by_show = {
        show.id: added for show, added in await show_membership_repo.list_with_added_at(db, user_id)
    }

    shows = list(shows_by_id.values())
    genres_by_show, networks_by_id, wcs_by_id = await hydrate_show_refs(db, shows)
    last_watched = await episode_watch_repo.latest_watched_per_show(
        db, user_id=user_id, show_ids=show_ids
    )
    last_aired = await episode_repo.latest_aired_per_show(db, show_ids, today_d)
    aired_counts = await episode_repo.count_aired_per_show(db, show_ids, today_d)
    total_counts = await episode_repo.count_per_show(db, show_ids)
    watched_counts = await episode_watch_repo.count_watched_per_show(
        db, user_id=user_id, show_ids=show_ids
    )

    watched_ep_ids = await episode_watch_repo.watched_in(
        db, user_id=user_id, episode_ids=[ep.id for ep in episodes]
    )

    entries: list[WatchNextEntry] = []
    for ep in episodes:
        show = shows_by_id.get(ep.show_id)
        if show is None:  # pragma: no cover  -- defensive: FK cascade prevents this
            continue
        entries.append(
            WatchNextEntry(
                show=build_show_summary_from_refs(
                    show,
                    genres_by_show=genres_by_show,
                    networks_by_id=networks_by_id,
                    wcs_by_id=wcs_by_id,
                ),
                episode=_episode_to_out(ep, watched=ep.id in watched_ep_ids),
                last_watched_at=last_watched.get(ep.show_id),
                last_aired=last_aired.get(ep.show_id),
                watched_episode_count=watched_counts.get(ep.show_id, 0),
                aired_episode_count=aired_counts.get(ep.show_id, 0),
                upcoming_episode_count=(
                    total_counts.get(ep.show_id, 0) - aired_counts.get(ep.show_id, 0)
                ),
                added_at=added_at_by_show.get(ep.show_id),
            )
        )

    return sort_watch_next(entries, sort)


async def list_upcoming(
    db: AsyncSession,
    *,
    user_id: UUID,
    sort: UpcomingSort = "airdate_asc",
    today: date | None = None,
) -> list[UpcomingEntry]:
    """Per show in My Shows, the earliest episode whose airdate is in the
    future. Shows with no scheduled future episodes are omitted."""
    today_d = today if today is not None else date.today()
    episodes = await episode_repo.earliest_future_per_show(db, user_id=user_id, today=today_d)
    if not episodes:
        return []

    show_ids = list({ep.show_id for ep in episodes})
    shows_by_id: dict[int, Show] = {}
    for sid in show_ids:
        show = await show_repo.get_by_id(db, sid)
        if show is not None:
            shows_by_id[sid] = show

    shows = list(shows_by_id.values())
    genres_by_show, networks_by_id, wcs_by_id = await hydrate_show_refs(db, shows)
    aired_counts = await episode_repo.count_aired_per_show(db, show_ids, today_d)
    total_counts = await episode_repo.count_per_show(db, show_ids)
    watched_counts = await episode_watch_repo.count_watched_per_show(
        db, user_id=user_id, show_ids=show_ids
    )
    added_at_by_show = {
        show.id: added for show, added in await show_membership_repo.list_with_added_at(db, user_id)
    }

    watched_ep_ids = await episode_watch_repo.watched_in(
        db, user_id=user_id, episode_ids=[ep.id for ep in episodes]
    )

    entries: list[UpcomingEntry] = []
    for ep in episodes:
        show = shows_by_id.get(ep.show_id)
        if show is None:  # pragma: no cover  -- defensive: FK cascade prevents this
            continue
        entries.append(
            UpcomingEntry(
                show=build_show_summary_from_refs(
                    show,
                    genres_by_show=genres_by_show,
                    networks_by_id=networks_by_id,
                    wcs_by_id=wcs_by_id,
                ),
                episode=_episode_to_out(ep, watched=ep.id in watched_ep_ids),
                watched_episode_count=watched_counts.get(ep.show_id, 0),
                aired_episode_count=aired_counts.get(ep.show_id, 0),
                upcoming_episode_count=(
                    total_counts.get(ep.show_id, 0) - aired_counts.get(ep.show_id, 0)
                ),
                added_at=added_at_by_show.get(ep.show_id),
            )
        )

    return sort_upcoming(entries, sort)
