"""Service-level tests for reconciliation_service (NEU-1030)."""

import json
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.fixtures.spines import mirror_spine
from tvbf.app.models import (
    ActivityEvent,
    UserEpisodeRating,
    UserEpisodeWatch,
    UserShowRating,
    UserShowWatch,
)
from tvbf.app.services import reconciliation_service as rs
from tvbf.jobs.reconcile import dumps
from tvbf.tvmaze.models import Episode, Show


async def _seed_show(session, *, show_id: int, name: str = "Recon Show") -> Show:
    show = Show(id=show_id, name=name, tvmaze_updated=1)
    session.add(show)
    await session.flush()
    await mirror_spine(session)
    return show


async def _seed_episode(session, *, show_id: int, episode_id: int, number: int = 1) -> Episode:
    ep = Episode(id=episode_id, show_id=show_id, season=1, number=number)
    session.add(ep)
    await session.flush()
    await mirror_spine(session)
    return ep


def _show_entry(snapshot, user_id, show_id):
    (user,) = [u for u in snapshot["users"] if u["user_id"] == str(user_id)]
    (entry,) = [s for s in user["shows"] if s["show_id"] == show_id]
    return entry


@pytest.mark.asyncio
async def test_snapshot_counts_every_metric_per_user_per_show(session, make_user):
    user = await make_user(email="rc1@example.com")
    show = await _seed_show(session, show_id=9200101)
    one = await _seed_episode(session, show_id=show.id, episode_id=9200201)
    two = await _seed_episode(session, show_id=show.id, episode_id=9200202, number=2)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=one.id))
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=two.id))
    session.add(UserShowRating(user_id=user.id, show_id=show.id, stars=Decimal("4.0")))
    session.add(UserEpisodeRating(user_id=user.id, episode_id=one.id, stars=Decimal("3.0")))
    session.add(
        ActivityEvent(actor_id=user.id, verb="watched", target_type="show", target_id=show.id)
    )
    session.add(
        ActivityEvent(actor_id=user.id, verb="watched", target_type="episode", target_id=one.id)
    )
    await session.commit()

    snapshot = await rs.build_snapshot(session)

    entry = _show_entry(snapshot, user.id, show.id)
    assert entry["tracked_shows"] == 1
    assert entry["episode_watches"] == 2
    assert entry["show_ratings"] == 1
    assert entry["episode_ratings"] == 1
    # Both the show-target and the episode-target event resolve to this show.
    assert entry["activity_events"] == 2
    assert snapshot["totals"]["episode_watches"] == 2
    assert snapshot["totals"]["users"] == 1
    assert snapshot["spine"] == "tvmaze"


@pytest.mark.asyncio
async def test_snapshot_separates_users_and_shows(session, make_user):
    one = await make_user(email="rc2a@example.com")
    two = await make_user(email="rc2b@example.com")
    show_a = await _seed_show(session, show_id=9200102, name="A")
    show_b = await _seed_show(session, show_id=9200103, name="B")
    ep_a = await _seed_episode(session, show_id=show_a.id, episode_id=9200203)
    ep_b = await _seed_episode(session, show_id=show_b.id, episode_id=9200204)
    session.add(UserEpisodeWatch(user_id=one.id, episode_id=ep_a.id))
    session.add(UserEpisodeWatch(user_id=two.id, episode_id=ep_a.id))
    session.add(UserEpisodeWatch(user_id=two.id, episode_id=ep_b.id))
    await session.commit()

    snapshot = await rs.build_snapshot(session)

    assert _show_entry(snapshot, one.id, show_a.id)["episode_watches"] == 1
    assert _show_entry(snapshot, two.id, show_a.id)["episode_watches"] == 1
    assert _show_entry(snapshot, two.id, show_b.id)["episode_watches"] == 1
    assert snapshot["totals"]["episode_watches"] == 3


@pytest.mark.asyncio
async def test_snapshot_includes_a_user_with_nothing_recorded(session, make_user):
    """A user who lost their last row must not vanish from both sides of a diff."""
    user = await make_user(email="rc3@example.com")
    await session.commit()

    snapshot = await rs.build_snapshot(session)

    (entry,) = [u for u in snapshot["users"] if u["user_id"] == str(user.id)]
    assert entry["shows"] == []
    assert entry["totals"]["episode_watches"] == 0
    assert snapshot["totals"]["users"] == 1


@pytest.mark.asyncio
async def test_snapshot_is_byte_stable_across_runs(session, make_user):
    """ "Diffable artifact" means byte-identical for an unchanged database."""
    user = await make_user(email="rc4@example.com")
    for offset, show_id in enumerate((9200106, 9200104, 9200105)):
        show = await _seed_show(session, show_id=show_id)
        ep = await _seed_episode(session, show_id=show.id, episode_id=9200210 + offset)
        session.add(UserEpisodeWatch(user_id=user.id, episode_id=ep.id))
    await session.commit()

    first = dumps(await rs.build_snapshot(session))
    second = dumps(await rs.build_snapshot(session))

    assert first == second
    # Shows are ordered by id, not by insertion or by whatever Postgres returns.
    (entry,) = [u for u in json.loads(first)["users"] if u["user_id"] == str(user.id)]
    assert [s["show_id"] for s in entry["shows"]] == [9200104, 9200105, 9200106]


@pytest.mark.asyncio
async def test_identical_snapshots_compare_clean(session, make_user):
    user = await make_user(email="rc5@example.com")
    show = await _seed_show(session, show_id=9200107)
    ep = await _seed_episode(session, show_id=show.id, episode_id=9200207)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=ep.id))
    await session.commit()

    baseline = await rs.build_snapshot(session)
    current = await rs.build_snapshot(session)

    assert rs.compare(baseline, current) == []


@pytest.mark.asyncio
async def test_a_deleted_row_is_reported_with_its_user_and_show(session, make_user):
    """The ticket's acceptance criterion, stated literally."""
    user = await make_user(email="rc6@example.com")
    show = await _seed_show(session, show_id=9200108, name="Deleted From Here")
    one = await _seed_episode(session, show_id=show.id, episode_id=9200208)
    two = await _seed_episode(session, show_id=show.id, episode_id=9200209, number=2)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=one.id))
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=two.id))
    await session.commit()
    baseline = await rs.build_snapshot(session)

    await session.execute(
        text("DELETE FROM app.user_episode_watch WHERE episode_id = :e"), {"e": two.id}
    )
    await session.commit()

    discrepancies = rs.compare(baseline, await rs.build_snapshot(session))

    assert len(discrepancies) == 1
    (found,) = discrepancies
    assert found["metric"] == "episode_watches"
    assert found["user_id"] == str(user.id)
    assert found["show_id"] == show.id
    assert found["baseline"] == 2
    assert found["current"] == 1
    assert found["delta"] == -1

    (line,) = await rs.describe(session, discrepancies)
    assert "LOST 1 episode_watches" in line
    assert "rc6@example.com" in line
    assert "Deleted From Here" in line
    assert str(show.id) in line


@pytest.mark.asyncio
async def test_a_gained_row_fails_too_and_says_so(session, make_user):
    user = await make_user(email="rc7@example.com")
    show = await _seed_show(session, show_id=9200109, name="Gained Here")
    ep = await _seed_episode(session, show_id=show.id, episode_id=9200211)
    await session.commit()
    baseline = await rs.build_snapshot(session)

    session.add(UserEpisodeWatch(user_id=user.id, episode_id=ep.id))
    await session.commit()

    discrepancies = rs.compare(baseline, await rs.build_snapshot(session))

    assert [d["delta"] for d in discrepancies] == [1]
    (line,) = await rs.describe(session, discrepancies)
    assert "GAINED 1 episode_watches" in line
    assert "Gained Here" in line


@pytest.mark.asyncio
async def test_a_whole_user_disappearing_is_reported(session, make_user):
    user = await make_user(email="rc8@example.com")
    show = await _seed_show(session, show_id=9200110)
    ep = await _seed_episode(session, show_id=show.id, episode_id=9200212)
    session.add(UserShowWatch(user_id=user.id, show_id=show.id))
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=ep.id))
    await session.commit()
    baseline = await rs.build_snapshot(session)

    await session.execute(text("DELETE FROM app.user WHERE id = :u"), {"u": user.id})
    await session.commit()

    discrepancies = rs.compare(baseline, await rs.build_snapshot(session))

    # The rows *and* the account itself, so the report says what actually went.
    assert {d["metric"] for d in discrepancies} == {
        "tracked_shows",
        "episode_watches",
        "user_accounts",
    }
    assert all(d["current"] == 0 and d["user_id"] == str(user.id) for d in discrepancies)
    # The user is gone, so the line has to fall back to the id rather than blow up.
    lines = await rs.describe(session, discrepancies)
    assert all("<deleted user" in line for line in lines)


@pytest.mark.asyncio
async def test_a_watch_whose_episode_vanished_lands_in_the_null_show_bucket(session, make_user):
    """Exactly the loss shape a cutover could produce, so it must not go missing.

    The joins are LEFT joins for this case alone; an INNER join would drop the
    orphaned watch and the totals would reconcile clean while data was gone.
    """
    user = await make_user(email="rc9@example.com")
    show = await _seed_show(session, show_id=9200111)
    ep = await _seed_episode(session, show_id=show.id, episode_id=9200213)
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=ep.id))
    await session.commit()

    # Drop the episode out from under the watch, leaving the app row orphaned.
    # The constraint references `catalog` since NEU-1046; the episode has to go
    # from both spines, because the snapshot is taken against `tvmaze` and an
    # episode still standing there would land the watch in its own show bucket
    # rather than the null one this test is about.
    fk_name = (
        await session.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'app.user_episode_watch'::regclass AND contype = 'f' "
                "AND confrelid = 'catalog.episode'::regclass"
            )
        )
    ).scalar_one()
    await session.execute(text(f"ALTER TABLE app.user_episode_watch DROP CONSTRAINT {fk_name}"))
    await session.execute(text("DELETE FROM tvmaze.episode WHERE id = :e"), {"e": ep.id})
    await session.execute(text("DELETE FROM catalog.episode WHERE id = :e"), {"e": ep.id})
    await session.commit()
    try:
        snapshot = await rs.build_snapshot(session)

        entry = _show_entry(snapshot, user.id, None)
        assert entry["episode_watches"] == 1
        assert snapshot["totals"]["episode_watches"] == 1

        (line,) = await rs.describe(
            session,
            [
                {
                    "user_id": str(user.id),
                    "show_id": None,
                    "metric": "episode_watches",
                    "baseline": 1,
                    "current": 0,
                    "delta": -1,
                }
            ],
        )
        assert "no show" in line
    finally:
        await session.execute(text("DELETE FROM app.user_episode_watch"))
        await session.execute(
            text(
                f"ALTER TABLE app.user_episode_watch ADD CONSTRAINT {fk_name} "
                "FOREIGN KEY (episode_id) REFERENCES catalog.episode(id) ON DELETE CASCADE"
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_unknown_spine_is_rejected_before_it_reaches_sql(session):
    with pytest.raises(rs.UnknownSpine):
        await rs.build_snapshot(session, spine="tvmaze; DROP TABLE app.user --")


@pytest.mark.asyncio
async def test_a_user_with_no_rows_at_all_is_still_caught_when_they_vanish(session, make_user):
    """The regression the zero-row union exists for.

    Such a user contributes no (user, show, metric) key, so a diff that walked
    only `shows` would report nothing at all when the account disappeared.
    """
    user = await make_user(email="rc10@example.com")
    await session.commit()
    baseline = await rs.build_snapshot(session)
    assert [u for u in baseline["users"] if u["user_id"] == str(user.id)][0]["shows"] == []

    await session.execute(text("DELETE FROM app.user WHERE id = :u"), {"u": user.id})
    await session.commit()

    discrepancies = rs.compare(baseline, await rs.build_snapshot(session))

    assert [d["metric"] for d in discrepancies] == ["user_accounts"]
    (found,) = discrepancies
    assert found["user_id"] == str(user.id)
    assert found["delta"] == -1
    (line,) = await rs.describe(session, discrepancies)
    assert "LOST 1 user_accounts" in line


@pytest.mark.asyncio
async def test_a_new_user_appearing_is_caught_too(session, make_user):
    baseline = await rs.build_snapshot(session)

    user = await make_user(email="rc11@example.com")
    await session.commit()

    (found,) = rs.compare(baseline, await rs.build_snapshot(session))
    assert found["metric"] == "user_accounts"
    assert found["user_id"] == str(user.id)
    assert found["delta"] == 1


def test_the_spine_selects_which_catalog_schema_the_joins_read():
    """`--spine catalog` is the post-cutover path; prove it changes the SQL.

    It cannot be run end to end yet — `catalog` has no `episode` table until the
    catalog tables land — so this asserts the parameterisation itself, which is
    the part that could silently be a no-op.
    """
    tvmaze = rs._queries("tvmaze")
    catalog = rs._queries("catalog")

    assert "tvmaze.episode" in tvmaze["episode_watches"]
    assert "catalog.episode" in catalog["episode_watches"]
    assert "catalog.episode" in catalog["activity_events"]
    assert "tvmaze" not in catalog["episode_ratings"]
    # Spine-free metrics are identical either way.
    assert tvmaze["tracked_shows"] == catalog["tracked_shows"]
