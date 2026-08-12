"""CLI-level tests for `python -m tvbf.jobs.episode_repoint` (NEU-1126).

These exercise `run()` rather than `main()`, matching `test_season_dedupe_cli.py`:
`main` is a wrapper whose `asyncio.run` would rebuild the event loop under the
shared engine's pooled connections. What matters here is the **exit code** and
what lands on **stdout**, because those are the two contracts — a scripted cutover
check reads the first and a reviewer reads the second.

The pass itself is covered against the database in
`tests/integration/tmdb/test_episode_repoint.py`, which is the right layer for it.
"""

import json

import pytest

from tvbf.catalog import models as cm
from tvbf.jobs.episode_repoint import _parse_args, run

_ID = 9_950_000


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


async def _seed_duplicate(session) -> int:
    """One matched show carrying a copied episode the ingest already superseded."""
    show_id = _next_id()
    session.add(cm.Show(id=show_id, name="CLI Repoint", tmdb_id=1396))
    await session.flush()
    for tmdb_id in (None, 62085):
        session.add(
            cm.Episode(
                id=_next_id(),
                show_id=show_id,
                season_number=1,
                episode_number=1,
                tmdb_id=tmdb_id,
            )
        )
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
    assert report["repointable"] == 1
    assert report["kept_under_unmatched_show"] == 0
    assert report["still_doubled"] == []


@pytest.mark.asyncio
async def test_report_writes_nothing_and_runs_before_the_ingest(session, capsys):
    """`report` carries no ingest floor on purpose: it is the half you run to
    decide, including on a database where the pass would refuse."""
    await _seed_duplicate(session)

    await run(_parse_args(["report"]))
    capsys.readouterr()

    await run(_parse_args(["report"]))
    assert json.loads(capsys.readouterr().out)["repointable"] == 1


@pytest.mark.asyncio
async def test_repoint_refuses_before_the_ingest_and_exits_one(session, capsys):
    """The seeded catalog is nowhere near the 150,000-show floor, so this is the
    guard's own path — a logged line and exit 1, not a stack trace."""
    await _seed_duplicate(session)

    assert await run(_parse_args(["repoint"])) == 1

    # Nothing on stdout: this half has no artifact, and the refusal is a log line.
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_limit_is_parsed(session):
    assert _parse_args(["repoint", "--limit", "5"]).limit == 5
    assert _parse_args(["repoint"]).limit is None
