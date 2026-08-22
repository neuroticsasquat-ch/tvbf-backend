"""CLI-level tests for `python -m tvbf.jobs.recommendations_backfill` (NEU-1052).

These exercise `run()` rather than `main()`, matching every other job test here:
`main` is a wrapper whose `asyncio.run` would rebuild the event loop under the
shared engine's pooled connections. What matters is the **exit code** and what
lands on **stdout**, because those are the two contracts — the process is the
run, and the report has to survive `ssh 'docker exec -i ...'` intact.

Only `report` is covered: it is the half with a stdout contract and the half that
needs no TMDB credential. The pass itself is covered against a mocked upstream in
`tests/integration/tmdb/test_recommendations_backfill.py`, which is the right
layer for it.
"""

import json
from datetime import UTC, datetime

import pytest

from tvbf.app.models import UserShowWatch
from tvbf.catalog import models as cm
from tvbf.jobs.recommendations_backfill import _parse_args, run

_ID = 9_600_000


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


async def _seed_tracked_show(session, make_user, *, name: str = "CLI Show") -> int:
    """A mirrored show a user tracks and that has no recommendations — exactly
    what the report's list exists to surface."""
    user = await make_user(email=f"recscli{_next_id()}@example.com")
    show_id = _next_id()
    session.add(
        cm.Show(
            id=show_id,
            name=name,
            tmdb_id=_next_id(),
            popularity=42.0,
            tmdb_synced_at=datetime(2026, 8, 11, tzinfo=UTC),
        )
    )
    await session.flush()
    session.add(UserShowWatch(user_id=user.id, show_id=show_id))
    await session.commit()
    return show_id


@pytest.mark.asyncio
async def test_report_writes_only_json_to_stdout(session, make_user, capsys):
    show_id = await _seed_tracked_show(session, make_user)

    assert await run(_parse_args(["report"])) == 0

    # Parsed, so nothing but the artifact reached stdout — logs go to stderr,
    # which is what lets this be redirected over `ssh docker exec`.
    report = json.loads(capsys.readouterr().out)
    (row,) = [r for r in report["user_touched_without_recommendations"] if r["show_id"] == show_id]
    assert (row["users"], row["stamped"]) == (1, False)
    assert report["totals"]["shows_remaining"] >= 1


@pytest.mark.asyncio
async def test_report_exits_zero_with_nothing_to_report(session, capsys):
    # Empty is the goal, not a failure — the exit code says the report ran, and
    # the numbers say where the pass got to.
    assert await run(_parse_args(["report"])) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["user_touched_without_recommendations"] == []
    assert report["rows"]["rows_stored"] == 0
