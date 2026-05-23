from decimal import Decimal

from sqlalchemy import select

from tvbf.tvmaze import models as m
from tvbf.tvmaze.api_payloads import TVMazeEpisode, TVMazeShow
from tvbf.tvmaze.upsert import upsert_episodes, upsert_show


def _show_payload(**overrides):
    base = {
        "id": 1,
        "name": "Foo",
        "updated": 1,
        "network": None,
        "webChannel": None,
        "genres": [],
        "_embedded": {"episodes": [], "seasons": []},
    }
    base.update(overrides)
    return base


async def test_upsert_show_persists_rating_average(session):
    payload = TVMazeShow.model_validate(_show_payload(id=700, rating={"average": 8.5}))
    await upsert_show(session, payload)
    await session.commit()

    row = (await session.execute(select(m.Show).where(m.Show.id == 700))).scalar_one()
    assert row.rating_average == Decimal("8.5")


async def test_upsert_show_with_no_rating_persists_null(session):
    payload = TVMazeShow.model_validate(_show_payload(id=701, rating={"average": None}))
    await upsert_show(session, payload)
    await session.commit()

    row = (await session.execute(select(m.Show).where(m.Show.id == 701))).scalar_one()
    assert row.rating_average is None


async def test_upsert_episodes_persists_mixed_rating_average(session):
    session.add(m.Show(id=702, name="S", tvmaze_updated=1))
    await session.flush()
    session.add(m.Season(id=7020, show_id=702, number=1))
    await session.commit()

    eps = [
        TVMazeEpisode.model_validate(
            {"id": 70001, "season": 1, "number": 1, "rating": {"average": 7.2}}
        ),
        TVMazeEpisode.model_validate(
            {"id": 70002, "season": 1, "number": 2, "rating": {"average": None}}
        ),
        TVMazeEpisode.model_validate({"id": 70003, "season": 1, "number": 3}),
    ]
    await upsert_episodes(session, show_id=702, episodes=eps)
    await session.commit()

    rows = (
        (
            await session.execute(
                select(m.Episode).where(m.Episode.show_id == 702).order_by(m.Episode.id)
            )
        )
        .scalars()
        .all()
    )
    by_id = {r.id: r for r in rows}
    assert by_id[70001].rating_average == Decimal("7.2")
    assert by_id[70002].rating_average is None
    assert by_id[70003].rating_average is None
