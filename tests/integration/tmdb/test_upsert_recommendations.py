"""Writing `catalog.show_recommendation` from a payload (NEU-1052).

The similar-shows surface is a list somebody else ranked, so almost every
property worth asserting here is about *not* improvising: the order is TMDB's,
the list is replaced rather than merged, and an entry we cannot resolve is
dropped rather than guessed at or renumbered around.

The one thing that is ours is the `None` / `[]` distinction every namespace
writer in `upsert.py` observes — a fetch that did not ask must not empty a
surface it never mentioned.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select

from tests.fixtures.tmdb.series_factory import make_recommendations, make_series
from tvbf.catalog import models as m
from tvbf.tmdb.api_payloads import TMDBSeries
from tvbf.tmdb.upsert import (
    mark_series_synced,
    upsert_series_payload,
    write_series_recommendations,
)

# Clear of the browse fixtures' catalog, so no assertion here can collide with a
# seeded show.
_SOURCE = 7_700_001


async def _write(session, payload: dict) -> int:
    show_id = await upsert_series_payload(session, TMDBSeries.model_validate(payload))
    await session.commit()
    return show_id


async def _mirror_targets(session, tmdb_ids: list[int], **overrides) -> dict[int, int]:
    """Shows the recommendations can point at, keyed `{tmdb_id: surrogate id}`."""
    ids = {}
    for offset, tmdb_id in enumerate(tmdb_ids):
        show_id = _SOURCE + 1_000 + offset
        session.add(m.Show(id=show_id, name=f"Target {tmdb_id}", tmdb_id=tmdb_id, **overrides))
        ids[tmdb_id] = show_id
    await session.flush()
    await session.commit()
    return ids


async def _stored(session, show_id: int) -> list[tuple[int, int]]:
    rows = await session.execute(
        select(m.ShowRecommendation.rank, m.ShowRecommendation.target_show_id)
        .where(m.ShowRecommendation.source_show_id == show_id)
        .order_by(m.ShowRecommendation.rank)
    )
    return [(row.rank, row.target_show_id) for row in rows]


class TestTheStoredList:
    async def test_twenty_entries_are_stored_in_tmdbs_order(self, session):
        """AC: a show with recommendations stores 20 ranked rows.

        Twenty because that is what one page of the appended namespace carries —
        measured as binary, 20 or 0, never a handful.
        """
        targets = await _mirror_targets(session, list(range(9_100, 9_120)))
        payload = make_series(9_001, seasons=0, append_seasons=False)
        payload["recommendations"] = make_recommendations(list(targets))

        show_id = await _write(session, payload)

        assert await _stored(session, show_id) == [
            (rank, targets[tmdb_id]) for rank, tmdb_id in enumerate(targets, start=1)
        ]

    async def test_a_re_ingest_replaces_the_list_rather_than_merging_it(self, session):
        """AC, and the reason the write is delete-then-insert.

        TMDB's ranking is a total order. Upserting rank by rank would leave a
        shorter new list wearing the tail of the old one — two vintages of one
        ordering interleaved, which is worse than either.
        """
        targets = await _mirror_targets(session, [9_201, 9_202, 9_203])
        payload = make_series(9_002, seasons=0, append_seasons=False)
        payload["recommendations"] = make_recommendations([9_201, 9_202, 9_203])
        show_id = await _write(session, payload)

        payload["recommendations"] = make_recommendations([9_203])
        await _write(session, payload)

        assert await _stored(session, show_id) == [(1, targets[9_203])]


class TestTheNamespaceRule:
    async def test_an_absent_namespace_leaves_the_stored_list_alone(self, session):
        """A delta fetching something narrower must not empty a surface it never
        asked about — the rule `prune_seasons` exists to enforce one level up."""
        targets = await _mirror_targets(session, [9_301])
        payload = make_series(9_003, seasons=0, append_seasons=False)
        payload["recommendations"] = make_recommendations([9_301])
        show_id = await _write(session, payload)

        del payload["recommendations"]
        await _write(session, payload)

        assert await _stored(session, show_id) == [(1, targets[9_301])]

    async def test_an_empty_namespace_clears_the_stored_list(self, session):
        """`[]` is upstream saying it has none, which is a fact and does apply —
        the distinction the writers spend, and the reason the parser keeps
        `None` and `[]` apart."""
        await _mirror_targets(session, [9_401])
        payload = make_series(9_004, seasons=0, append_seasons=False)
        payload["recommendations"] = make_recommendations([9_401])
        show_id = await _write(session, payload)

        payload["recommendations"] = make_recommendations([])
        await _write(session, payload)

        assert await _stored(session, show_id) == []


class TestWhatIsDropped:
    async def test_a_target_we_do_not_mirror_is_dropped_and_leaves_its_rank_behind(self, session):
        """AC: no stored row points at a missing show.

        Measured this is a no-op — 502 of 502 sampled targets resolved — and it
        stays as a guard because a series TMDB created this morning is not
        mirrored until tonight's delta. The surviving ranks are TMDB's own, gap
        and all: renumbering would make `rank` mean *our position after
        filtering*, which is not a thing anybody ranked.
        """
        targets = await _mirror_targets(session, [9_501, 9_503])
        payload = make_series(9_005, seasons=0, append_seasons=False)
        payload["recommendations"] = make_recommendations([9_501, 9_502, 9_503])

        show_id = await _write(session, payload)

        assert await _stored(session, show_id) == [(1, targets[9_501]), (3, targets[9_503])]

    async def test_a_tombstoned_target_is_dropped(self, session):
        """AC: no stored row points at a tombstoned show.

        The read path filters `deleted_upstream_at` as well, and neither is
        load-bearing alone: that filter covers a show tombstoned *after* this
        list was written, and this one keeps the stored list honest meanwhile. A
        resurrected show comes back on the source show's next refresh, which
        every ingest and every delta performs.
        """
        await _mirror_targets(
            session, [9_601], deleted_upstream_at=datetime(2026, 8, 1, tzinfo=UTC)
        )
        payload = make_series(9_006, seasons=0, append_seasons=False)
        payload["recommendations"] = make_recommendations([9_601])

        show_id = await _write(session, payload)

        assert await _stored(session, show_id) == []

    async def test_a_show_recommended_alongside_itself_is_dropped(self, session):
        """A self-edge would render the show as its own "more like this" card."""
        payload = make_series(9_007, seasons=0, append_seasons=False)
        payload["recommendations"] = make_recommendations([9_007])

        show_id = await _write(session, payload)

        assert await _stored(session, show_id) == []

    async def test_a_target_listed_twice_is_one_edge(self, session):
        """`uq_show_recommendation_target` would otherwise refuse the second row
        and cost the whole show its write, so the writer collapses it first."""
        targets = await _mirror_targets(session, [9_701])
        payload = make_series(9_008, seasons=0, append_seasons=False)
        payload["recommendations"] = make_recommendations([9_701, 9_701])

        show_id = await _write(session, payload)

        assert await _stored(session, show_id) == [(1, targets[9_701])]


class TestTheSeam:
    async def test_the_seam_reports_what_it_dropped_as_well_as_what_it_wrote(self, session):
        """The two ways of ending at zero rows, kept apart at the writer.

        Upstream recommending nothing is normal and expected; upstream
        recommending twenty shows we do not mirror is the mirror being behind.
        Only the writer still has the payload in hand, so it is the only place
        the distinction can be drawn — a caller counting rows afterwards would
        read both as silence.
        """
        payload = make_series(9_011, seasons=0, append_seasons=False)
        payload["recommendations"] = make_recommendations([9_901, 9_902])

        show_id = await _write(session, payload)

        written = await write_series_recommendations(
            session, TMDBSeries.model_validate(payload), show_id=show_id
        )
        await session.commit()

        assert (written.offered, written.written, written.dropped) == (2, 0, 2)

    async def test_write_series_recommendations_writes_no_spine_row(self, session):
        """What the backfill actually depends on: the seam writes its one table.

        Asserted here rather than trusted, because "writes nothing else" is the
        whole reason the function exists separately from `upsert_series_payload`
        — the same guarantee `write_series_credits` makes at the credit grain.
        """
        targets = await _mirror_targets(session, [9_801])
        show_id = _SOURCE + 8
        session.add(m.Show(id=show_id, name="Already Mirrored", tmdb_id=9_009))
        await session.flush()
        await session.commit()
        before = (await session.execute(select(func.count()).select_from(m.Show))).scalar_one()

        payload = make_series(9_009, seasons=2, episodes_per_season=2)
        payload["recommendations"] = make_recommendations([9_801])
        written = await write_series_recommendations(
            session, TMDBSeries.model_validate(payload), show_id=show_id
        )
        await session.commit()

        assert (written.offered, written.written, written.dropped) == (1, 1, 0)
        assert await _stored(session, show_id) == [(1, targets[9_801])]
        assert (
            await session.execute(select(func.count()).select_from(m.Show))
        ).scalar_one() == before
        assert (await session.execute(select(func.count()).select_from(m.Season))).scalar_one() == 0
        assert (
            await session.execute(select(func.count()).select_from(m.Episode))
        ).scalar_one() == 0

    async def test_a_full_pass_stamps_the_recommendations_watermark(self, session):
        """`mark_series_synced` stamps all three, because every caller of it
        reaches it through `mirror_series`, which now appends the namespace by
        construction. That is what stops the backfill re-fetching shows the
        nightly delta has already covered."""
        show_id = _SOURCE + 9
        session.add(m.Show(id=show_id, name="Freshly Mirrored", tmdb_id=9_010))
        await session.flush()

        await mark_series_synced(session, show_id=show_id)
        await session.commit()

        row = (
            await session.execute(
                select(m.Show).where(m.Show.id == show_id).execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert row.tmdb_synced_at is not None
        assert row.credits_synced_at is not None
        assert row.recommendations_synced_at is not None
