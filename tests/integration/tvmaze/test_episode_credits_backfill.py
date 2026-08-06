from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update

from tvbf.tvmaze import models as m
from tvbf.tvmaze.episode_credits_backfill import run_episode_credits_backfill


def _episode(episode_id: int, *, number: int, guestcast=(), guestcrew=()):
    return {
        "id": episode_id,
        "season": 1,
        "number": number,
        "name": f"E{number}",
        "airdate": "",
        "airtime": "",
        "_embedded": {"guestcast": list(guestcast), "guestcrew": list(guestcrew)},
    }


def _person(person_id: int, name: str):
    return {"id": person_id, "name": name, "updated": 1}


class FakeClient:
    """Returns a canned episode list per season and records the call order."""

    def __init__(
        self,
        payloads: dict[int, list[dict]] | None = None,
        fail: set[int] | None = None,
        gone: set[int] | None = None,
    ):
        self._payloads = payloads or {}
        self._fail = fail or set()
        # `gone` raises 404 (the season is deleted upstream); `fail` raises 500
        # (upstream is broken). NEU-1006 makes the two behave differently.
        self._gone = gone or set()
        self.calls: list[int] = []

    async def get_season_episodes(self, season_id: int) -> list[dict]:
        self.calls.append(season_id)
        if season_id in self._fail or season_id in self._gone:
            status = 404 if season_id in self._gone else 500
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request(
                    "GET", f"https://api.tvmaze.com/seasons/{season_id}/episodes"
                ),
                response=httpx.Response(status),
            )
        return self._payloads.get(season_id, [])


async def _seed_show(session, show_id: int, seasons: list[tuple[int, int]]):
    """Insert a show plus `(season_id, number)` seasons, all unstamped."""
    session.add(m.Show(id=show_id, name=f"Show {show_id}", tvmaze_updated=1))
    await session.flush()
    for season_id, number in seasons:
        session.add(m.Season(id=season_id, show_id=show_id, number=number))
    await session.flush()


async def test_backfill_writes_credits_and_stamps_the_watermark(session):
    await _seed_show(session, 100, [(1000, 1)])
    session.add(m.Episode(id=5000, show_id=100, season_id=1000, season=1, number=1, name="E1"))
    run = m.IngestRun(id=uuid4(), kind="episode_credits_backfill", status="running")
    session.add(run)
    await session.commit()

    client = FakeClient(
        {
            1000: [
                _episode(
                    5000,
                    number=1,
                    guestcrew=[{"guestCrewType": "Director", "person": _person(900, "Ada")}],
                )
            ]
        }
    )
    result = await run_episode_credits_backfill(
        session_factory=lambda: session, client=client, run_id=run.id
    )

    assert client.calls == [1000]
    assert (result.seasons_processed, result.seasons_failed) == (1, 0)

    crew = (await session.execute(select(m.EpisodeCrew))).scalars().all()
    assert [(c.episode_id, c.sort_order) for c in crew] == [(5000, 0)]

    season = (
        await session.execute(
            select(m.Season).where(m.Season.id == 1000).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert season.credits_synced_at is not None


async def test_backfill_skips_already_stamped_seasons(session):
    """Resumability is the watermark, not an offset — a re-run redoes only what's NULL."""
    await _seed_show(session, 110, [(1100, 1), (1101, 2)])
    await session.execute(
        update(m.Season).where(m.Season.id == 1100).values(credits_synced_at=datetime.now(UTC))
    )
    run = m.IngestRun(id=uuid4(), kind="episode_credits_backfill", status="running")
    session.add(run)
    await session.commit()

    client = FakeClient()
    await run_episode_credits_backfill(
        session_factory=lambda: session, client=client, run_id=run.id
    )
    assert client.calls == [1101]


async def test_work_list_is_ordered_by_show_then_season_number(session):
    """A show's seasons are processed contiguously even when its ids are scattered.

    Season ids are not contiguous per show upstream. Under id ordering a
    long-running show would render a partial crew list for hours mid-run; under
    show ordering at most one show is ever half-populated.
    """
    await _seed_show(session, 120, [(1500, 1), (9500, 2)])
    await _seed_show(session, 121, [(1501, 1), (9501, 2)])
    run = m.IngestRun(id=uuid4(), kind="episode_credits_backfill", status="running")
    session.add(run)
    await session.commit()

    client = FakeClient()
    await run_episode_credits_backfill(
        session_factory=lambda: session, client=client, run_id=run.id
    )
    assert client.calls == [1500, 9500, 1501, 9501]


async def test_a_single_show_failing_wholesale_does_not_abort_the_run(session):
    """2,560 shows have 10+ seasons: per-season counting would abort on one dead show."""
    await _seed_show(session, 130, [(1300 + i, i) for i in range(1, 13)])
    await _seed_show(session, 131, [(1400, 1)])
    run = m.IngestRun(id=uuid4(), kind="episode_credits_backfill", status="running")
    session.add(run)
    await session.commit()

    client = FakeClient(fail={1300 + i for i in range(1, 13)})
    result = await run_episode_credits_backfill(
        session_factory=lambda: session, client=client, run_id=run.id, failure_threshold=10
    )

    assert result.seasons_failed == 12
    assert result.seasons_processed == 1
    assert 1400 in client.calls

    refreshed = (
        await session.execute(
            select(m.IngestRun)
            .where(m.IngestRun.id == run.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.status == "succeeded"


async def test_a_show_with_one_surviving_season_resets_the_counter(session):
    """The counter counts shows that fail *wholesale*, so a partial success clears it."""
    await _seed_show(session, 140, [(1600, 1)])  # fails
    await _seed_show(session, 141, [(1601, 1), (1602, 2)])  # one of two fails
    await _seed_show(session, 142, [(1603, 1)])  # fails
    run = m.IngestRun(id=uuid4(), kind="episode_credits_backfill", status="running")
    session.add(run)
    await session.commit()

    client = FakeClient(fail={1600, 1601, 1603})
    result = await run_episode_credits_backfill(
        session_factory=lambda: session, client=client, run_id=run.id, failure_threshold=2
    )

    # Without the reset, shows 140 and 141 would have tripped the threshold of 2
    # and show 142 would never have been reached.
    assert client.calls == [1600, 1601, 1602, 1603]
    assert (result.seasons_processed, result.seasons_failed) == (1, 3)


async def test_backfill_aborts_after_consecutive_failed_shows(session):
    await _seed_show(session, 150, [(1700, 1)])
    await _seed_show(session, 151, [(1701, 1)])
    await _seed_show(session, 152, [(1702, 1)])
    run = m.IngestRun(id=uuid4(), kind="episode_credits_backfill", status="running")
    session.add(run)
    await session.commit()

    client = FakeClient(fail={1700, 1701, 1702})
    result = await run_episode_credits_backfill(
        session_factory=lambda: session, client=client, run_id=run.id, failure_threshold=2
    )

    assert client.calls == [1700, 1701]  # aborted before the third show
    assert (result.seasons_processed, result.seasons_failed) == (0, 2)

    refreshed = (
        await session.execute(
            select(m.IngestRun)
            .where(m.IngestRun.id == run.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.status == "failed"
    assert refreshed.error is not None
    assert "consecutive shows" in refreshed.error


async def test_progress_counts_seasons_in_the_shows_columns(session):
    await _seed_show(session, 160, [(1800, 1), (1801, 2)])
    run = m.IngestRun(id=uuid4(), kind="episode_credits_backfill", status="running")
    session.add(run)
    await session.commit()

    client = FakeClient(fail={1801})
    await run_episode_credits_backfill(
        session_factory=lambda: session, client=client, run_id=run.id
    )

    refreshed = (
        await session.execute(
            select(m.IngestRun)
            .where(m.IngestRun.id == run.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.shows_processed == 1
    assert refreshed.shows_failed == 1
    assert refreshed.last_update_cursor is None


@pytest.mark.parametrize("kind", ["episode_credits_backfill"])
async def test_backfill_run_kind_is_admitted_by_the_check_constraint(session, kind):
    run = m.IngestRun(id=uuid4(), kind=kind, status="running")
    session.add(run)
    await session.commit()

    row = (await session.execute(select(m.IngestRun).where(m.IngestRun.id == run.id))).scalar_one()
    assert row.kind == kind


async def test_shows_whose_every_season_is_gone_do_not_abort_the_run(session):
    """NEU-1006 at the per-show grain — the site the spec flagged as easy to miss.

    Three shows, every season 404, threshold 2. Under the old rule the run
    aborted at the second show; now it walks the whole list. This pass produced
    245 such 404s in NEU-961's prod run.
    """
    await _seed_show(session, 160, [(1800, 1)])
    await _seed_show(session, 161, [(1801, 1)])
    await _seed_show(session, 162, [(1802, 1)])
    run = m.IngestRun(id=uuid4(), kind="episode_credits_backfill", status="running")
    session.add(run)
    await session.commit()

    client = FakeClient(gone={1800, 1801, 1802})
    result = await run_episode_credits_backfill(
        session_factory=lambda: session, client=client, run_id=run.id, failure_threshold=2
    )

    assert client.calls == [1800, 1801, 1802], "every show must be attempted"
    assert (result.seasons_processed, result.seasons_failed) == (0, 3)

    refreshed = (
        await session.execute(
            select(m.IngestRun)
            .where(m.IngestRun.id == run.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.status == "succeeded"


async def test_a_show_mixing_gone_and_broken_seasons_still_counts(session):
    """Only a wholly-gone show is exempt. One real 500 makes the show count.

    Two such shows against a threshold of 2 must abort — otherwise a genuine
    outage hides behind a single 404 per show.
    """
    await _seed_show(session, 170, [(1900, 1), (1901, 2)])
    await _seed_show(session, 171, [(1902, 1), (1903, 2)])
    await _seed_show(session, 172, [(1904, 1)])
    run = m.IngestRun(id=uuid4(), kind="episode_credits_backfill", status="running")
    session.add(run)
    await session.commit()

    client = FakeClient(gone={1900, 1902}, fail={1901, 1903, 1904})
    result = await run_episode_credits_backfill(
        session_factory=lambda: session, client=client, run_id=run.id, failure_threshold=2
    )

    assert client.calls == [1900, 1901, 1902, 1903], "aborted before the third show"
    assert result.seasons_processed == 0

    refreshed = (
        await session.execute(
            select(m.IngestRun)
            .where(m.IngestRun.id == run.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.status == "failed"
