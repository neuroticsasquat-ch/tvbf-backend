"""CLI-level tests for `python -m tvbf.jobs.season_dedupe` (NEU-1119).

These exercise `run()` rather than `main()`, matching `test_episode_map_cli.py`:
`main` is a wrapper whose `asyncio.run` would rebuild the event loop under the
shared engine's pooled connections. What matters here is the **exit code** and
what lands on **stdout**, because those are the two contracts — a scripted
cutover check reads the first and a reviewer reads the second.

The pass itself is covered against the database in
`tests/integration/tmdb/test_season_dedupe.py`, which is the right layer for it.
"""

import json

import pytest

from tvbf.catalog import models as cm
from tvbf.jobs.season_dedupe import _parse_args, run

_ID = 9_900_000


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


async def _seed_duplicate(session) -> int:
    """One matched show carrying a copied season the ingest already superseded."""
    show_id = _next_id()
    session.add(cm.Show(id=show_id, name="CLI Dedupe", tmdb_id=1396))
    await session.flush()
    for tmdb_id in (None, 3572):
        session.add(cm.Season(id=_next_id(), show_id=show_id, season_number=1, tmdb_id=tmdb_id))
    await session.flush()
    await session.commit()
    return show_id


@pytest.mark.asyncio
async def test_report_writes_only_json_to_stdout(session, capsys):
    await _seed_duplicate(session)

    assert await run(_parse_args(["report"])) == 0

    # Parsed, so nothing but the artifact reached stdout — logs go to stderr,
    # which is what lets this be redirected over `ssh docker exec`.
    report = json.loads(capsys.readouterr().out)
    assert report["deletable_duplicates"] == 1
    assert report["kept_under_unmatched_show"] == 0
    assert report["still_doubled"] == []


@pytest.mark.asyncio
async def test_report_writes_nothing(session, capsys):
    await _seed_duplicate(session)

    await run(_parse_args(["report"]))
    capsys.readouterr()

    # Still one duplicate: `report` is the half that is safe against production.
    await run(_parse_args(["report"]))
    assert json.loads(capsys.readouterr().out)["deletable_duplicates"] == 1


@pytest.mark.asyncio
async def test_dedupe_clears_the_duplicates_and_exits_zero(session, capsys):
    await _seed_duplicate(session)

    assert await run(_parse_args(["dedupe"])) == 0

    capsys.readouterr()
    await run(_parse_args(["report"]))
    assert json.loads(capsys.readouterr().out)["deletable_duplicates"] == 0


@pytest.mark.asyncio
async def test_dedupe_writes_nothing_to_stdout(session, capsys):
    """Unlike `report`, this half has no artifact — its result is the exit code."""
    await _seed_duplicate(session)

    await run(_parse_args(["dedupe"]))

    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_limit_is_parsed(session):
    assert _parse_args(["dedupe", "--limit", "5"]).limit == 5
    assert _parse_args(["dedupe"]).limit is None
