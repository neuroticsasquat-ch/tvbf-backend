"""CLI-level tests for `python -m tvbf.jobs.coverage_gate` (NEU-1048).

These exercise `run()` rather than `main()`, matching `test_season_dedupe_cli.py`:
`main` is a wrapper whose `asyncio.run` would rebuild the event loop under the
shared engine's pooled connections. What matters here is the **exit code** and
what lands on **stdout**, because for this job those are the whole contract — the
exit code *is* the verdict, and the artifact on stdout is what travels over
`ssh 'docker exec ...'` and gets diffed against the previous run.

The criteria and the coverage comparison are covered against the database in
`tests/integration/tmdb/test_coverage_gate.py`, which is the right layer for them.
"""

import json

import pytest

from tvbf.app.models import User, UserShowWatch
from tvbf.catalog import models as cm
from tvbf.jobs.coverage_gate import _parse_args, run
from tvbf.tvmaze.models import Show as MazeShow

_ID = 9_910_000


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


async def _seed_show(session, *, tmdb_id: int | None) -> int:
    show_id = _next_id()
    session.add(MazeShow(id=show_id, name="CLI Gate", language="English", tvmaze_updated=0))
    await session.flush()
    session.add(cm.Show(id=show_id, name="CLI Gate", tmdb_id=tmdb_id))
    await session.flush()
    await session.commit()
    return show_id


async def _track(session, show_id: int) -> None:
    user = User(email="gate-cli@example.com", display_name="Gate", password_hash="x")
    session.add(user)
    await session.flush()
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()


@pytest.mark.asyncio
async def test_a_go_exits_zero_and_writes_only_json(session, capsys):
    await _seed_show(session, tmdb_id=1396)

    assert await run(_parse_args(["--min-ingested", "0"])) == 0

    # Parsed, so nothing but the artifact reached stdout — the verdict and every
    # criterion go to stderr, which is what lets this be redirected.
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "go"
    assert report["failed"] == []
    assert report["coverage"]["totals"]["tvmaze_shows"] == 1


@pytest.mark.asyncio
async def test_a_no_go_exits_one(session, capsys):
    """The exit code is the verdict — a scripted cutover check reads nothing else."""
    show_id = await _seed_show(session, tmdb_id=None)
    await _track(session, show_id)

    assert await run(_parse_args(["--min-ingested", "0"])) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "no-go"
    assert report["failed"] == ["user_touched_shows_resolved"]


@pytest.mark.asyncio
async def test_the_default_floor_is_the_real_one(session):
    """Nothing in production passes `--min-ingested`, so the default has to be the guard."""
    assert _parse_args([]).min_ingested >= 150_000


@pytest.mark.asyncio
async def test_the_artifact_is_byte_identical_across_runs(session, capsys):
    """Deterministic output is what makes `git diff` the regression check."""
    await _seed_show(session, tmdb_id=1396)

    await run(_parse_args(["--min-ingested", "0"]))
    first = capsys.readouterr().out
    await run(_parse_args(["--min-ingested", "0"]))

    assert capsys.readouterr().out == first


@pytest.mark.asyncio
async def test_the_gate_writes_nothing(session, capsys):
    """Reading is the whole job — run it as often as it takes to get to a go."""
    show_id = await _seed_show(session, tmdb_id=None)
    await _track(session, show_id)

    await run(_parse_args(["--min-ingested", "0"]))
    capsys.readouterr()

    assert await run(_parse_args(["--min-ingested", "0"])) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "no-go"
