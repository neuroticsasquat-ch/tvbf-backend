"""CLI-level tests for `python -m tvbf.jobs.episode_map` (NEU-1045).

These exercise `run()` rather than `main()`, matching `test_human_queue_cli.py`:
`main` is a wrapper whose `asyncio.run` would rebuild the event loop under the
shared engine's pooled connections. What matters here is the **exit code** and
what lands on **stdout**, because those are the two contracts — a scripted
cutover check reads the first and a reviewer reads the second.

Only `report` is covered: it is the half with a stdout contract, and it needs no
TMDB credential. The mapping pass is covered against upstream in
`tests/integration/tmdb/test_episode_map.py`, which is the right layer for it.
"""

import json

import pytest

from tvbf.app.models import UserEpisodeWatch
from tvbf.catalog import models as cm
from tvbf.jobs.episode_map import _parse_args, run

_ID = 9_700_000


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


async def _seed_unmapped_watch(session, make_user, *, name: str = "CLI Show") -> int:
    """A watched episode with no `tmdb_id` — exactly what the report exists to surface."""
    user = await make_user(email=f"emcli{_next_id()}@example.com")
    show_id = _next_id()
    session.add(cm.Show(id=show_id, name=name, tmdb_id=1396))
    await session.flush()

    episode_id = _next_id()
    session.add(
        cm.Episode(id=episode_id, show_id=show_id, season_number=1, episode_number=9, name="Odd")
    )
    await session.flush()
    session.add(UserEpisodeWatch(user_id=user.id, episode_id=episode_id))
    await session.commit()
    return episode_id


@pytest.mark.asyncio
async def test_report_writes_only_json_to_stdout(session, make_user, capsys):
    episode_id = await _seed_unmapped_watch(session, make_user)

    assert await run(_parse_args(["report"])) == 0

    # Parsed, so nothing but the artifact reached stdout — logs go to stderr,
    # which is what lets this be redirected over `ssh docker exec`.
    report = json.loads(capsys.readouterr().out)
    (row,) = [r for r in report["unmatched_user_data"] if r["episode_id"] == episode_id]
    assert row["watches"] == 1


@pytest.mark.asyncio
async def test_report_exits_zero_with_nothing_to_report(session, capsys):
    # Empty is the goal, not a failure: the cutover gate reads *what* it printed,
    # not whether it exited non-zero.
    assert await run(_parse_args(["report"])) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["unmatched_user_data"] == []
    assert report["totals"]["watched_episodes_unmapped"] == 0
