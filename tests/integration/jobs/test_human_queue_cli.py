"""CLI-level tests for `python -m tvbf.jobs.human_queue` (NEU-1044).

These exercise `run()` rather than `main()`, matching `test_reconcile_cli.py`:
`main` is a wrapper whose `asyncio.run` would rebuild the event loop under the
shared engine's pooled connections. What matters here is the **exit code** and
what lands on **stdout**, because those are the two contracts — a scripted
resolution reads the first and a reviewer reads the second.

`--no-candidates` throughout: the upstream half is covered in
`tests/integration/tmdb/test_human_queue.py`, and a CLI test that needed a TMDB
credential would be testing the wrong layer.
"""

import json

import pytest

from tvbf.app.models import UserShowWatch
from tvbf.catalog import models as cm
from tvbf.jobs.human_queue import _parse_args, run
from tvbf.tmdb.enrichment import MATCH_HUMAN, MATCH_TITLE_YEAR

_ID = 9_500_000


async def _seed_touched_show(session, make_user, *, name="CLI Show", **catalog_kwargs) -> int:
    global _ID
    _ID += 1
    user = await make_user(email=f"cli{_ID}@example.com")
    session.add(cm.Show(id=_ID, name=name, **catalog_kwargs))
    await session.flush()
    session.add(UserShowWatch(user_id=user.id, show_id=_ID))
    await session.commit()
    return _ID


async def _listing(capsys) -> list[dict]:
    assert await run(_parse_args(["list", "--no-candidates"])) == 0
    return json.loads(capsys.readouterr().out)


def _entry(report: list[dict], show_id: int) -> dict:
    (entry,) = [row for row in report if row["show_id"] == show_id]
    return entry


@pytest.mark.asyncio
async def test_list_writes_only_json_to_stdout(session, make_user, capsys):
    show_id = await _seed_touched_show(session, make_user, name="Discretion")

    report = await _listing(capsys)

    # Parsed, so nothing but the artifact reached stdout — logs go to stderr,
    # which is what lets this be redirected over `ssh docker exec`.
    assert _entry(report, show_id)["name"] == "Discretion"


@pytest.mark.asyncio
async def test_list_exits_zero_when_the_queue_is_empty(session, capsys):
    # An empty queue is the goal, not a failure: `list` is a report and the
    # cutover gate reads *what* it printed, not whether it exited non-zero.
    assert await run(_parse_args(["list", "--no-candidates"])) == 0
    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.asyncio
async def test_confirm_then_list_removes_the_row(session, make_user, capsys):
    show_id = await _seed_touched_show(session, make_user)
    assert _entry(await _listing(capsys), show_id)

    assert await run(_parse_args(["confirm", str(show_id), "12345"])) == 0

    assert [row for row in await _listing(capsys) if row["show_id"] == show_id] == []


@pytest.mark.asyncio
async def test_reject_then_list_removes_the_row(session, make_user, capsys):
    show_id = await _seed_touched_show(session, make_user)

    assert await run(_parse_args(["reject", str(show_id)])) == 0

    report = await _listing(capsys)
    assert [row for row in report if row["show_id"] == show_id] == []


@pytest.mark.asyncio
async def test_a_refused_resolution_exits_one(session, make_user, capsys):
    show_id = await _seed_touched_show(session, make_user)
    holder = await _seed_touched_show(
        session, make_user, tmdb_id=747, match_method=MATCH_TITLE_YEAR
    )

    assert await run(_parse_args(["confirm", str(show_id), "747"])) == 1

    # Nothing applied, and both rows still queued — a scripted caller that read
    # only the exit code would not have been misled.
    report = await _listing(capsys)
    assert _entry(report, show_id)["tmdb_id"] is None
    assert _entry(report, holder)["match_method"] == MATCH_TITLE_YEAR


@pytest.mark.asyncio
async def test_confirm_re_stamps_a_guess_that_was_right(session, make_user, capsys):
    show_id = await _seed_touched_show(
        session, make_user, tmdb_id=299737, match_method=MATCH_TITLE_YEAR
    )

    assert await run(_parse_args(["confirm", str(show_id), "299737"])) == 0

    # The verdict now lives in the database rather than in a ticket comment.
    row = (await session.execute(cm.Show.__table__.select().where(cm.Show.id == show_id))).one()
    assert (row.tmdb_id, row.match_method) == (299737, MATCH_HUMAN)
