"""CLI-level tests for `python -m tvbf.jobs.orphan_retire` (NEU-1146).

These exercise `run()` rather than `main()`, matching `test_episode_repoint_cli.py`
and `test_season_dedupe_cli.py`: `main` is a wrapper whose `asyncio.run` would
rebuild the event loop under the shared engine's pooled connections. What matters
here is the **exit code** and what lands on **stdout**, because those are the two
contracts — a scripted cutover check reads the first and a reviewer reads the
second.

This CLI's exit code carries more than its siblings'. Everywhere else, rows left
behind are the expected output; here they are the TV Maze data the ticket exists
to remove, so a completed run that left orphans standing exits **1**. That is
criterion 7, machine-checked, and it is what the frontend half is gated on.

The matcher and the pass are covered against the database in
`tests/integration/tmdb/test_orphan_retire.py`, which is the right layer for them.
"""

import json

import pytest
from sqlalchemy import text

from tvbf.catalog import models as cm
from tvbf.jobs.orphan_retire import _parse_args, run

_ID = 9_980_000


def _next_id() -> int:
    global _ID
    _ID += 1
    return _ID


async def _seed_orphan_and_twin(session) -> int:
    """A matched show carrying one orphan episode and the ingested row that supersedes it."""
    show_id = _next_id()
    session.add(cm.Show(id=show_id, name="CLI Retire", tmdb_id=1396))
    await session.flush()
    for tmdb_id in (None, 62085):
        session.add(
            cm.Episode(
                id=_next_id(),
                show_id=show_id,
                season_number=1,
                episode_number=1,
                name="Pilot",
                tmdb_id=tmdb_id,
            )
        )
    await session.flush()
    await session.commit()
    return show_id


@pytest.mark.asyncio
async def test_report_writes_only_json_to_stdout(session, capsys):
    await _seed_orphan_and_twin(session)

    assert await run(_parse_args(["report"])) == 0

    # Parsed, so nothing but the artifact reached stdout — logs go to stderr,
    # which is what lets this be redirected over `ssh docker exec`.
    report = json.loads(capsys.readouterr().out)
    assert report["orphan_episodes"] == 1
    assert report["to_delete"] == 0
    assert report["losses"] == []
    # Every key §5 requires a reviewer to be able to read, present by name.
    for key in (
        "by_cause",
        "by_tier",
        "by_tier_user_touched",
        "rejections",
        "links",
        "links_dropped_multiple_candidates",
        "loss_summary",
        "show_watches_to_create",
    ):
        assert key in report


@pytest.mark.asyncio
async def test_report_runs_below_the_ingest_floor_where_the_pass_refuses(session, capsys):
    """`report` carries no floor on purpose: it is the half you run to decide.

    It says so instead — a pre-ingest reading makes almost every orphan look
    like a deletion, which is the exact misread the floor exists to prevent.
    """
    await _seed_orphan_and_twin(session)

    assert await run(_parse_args(["report"])) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["ingested_shows"] < 150_000


@pytest.mark.asyncio
async def test_retire_refuses_below_the_ingest_floor_and_writes_no_json(session, capsys):
    await _seed_orphan_and_twin(session)

    assert await run(_parse_args(["retire"])) == 1

    # Nothing on stdout: `retire` is not the mode that produces an artifact, and
    # a half-written one would be worse than none.
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_the_exit_code_is_criterion_7(session, capsys, monkeypatch):
    """0 only when no orphan row survives at any grain — every sibling exits 0 on residue.

    The un-retirable orphan is a show `import_ne.show_resolution` still points at:
    that FK is NO ACTION and the staging rows are an import audit trail rather
    than ours to rewrite, so the pass skips it. It is the one way a *completed*
    run legitimately leaves criterion 7 unmet, which makes it the right subject
    for the exit code that scores it.
    """
    import tvbf.jobs.orphan_retire as cli

    monkeypatch.setattr(cli, "retire_orphans", _retire_with_no_floor)
    await _seed_orphan_and_twin(session)

    assert await run(_parse_args(["retire"])) == 0
    capsys.readouterr()

    await session.execute(text("CREATE SCHEMA IF NOT EXISTS import_ne"))
    await session.execute(
        text(
            "CREATE TABLE IF NOT EXISTS import_ne.show_resolution ("
            "  id bigserial PRIMARY KEY,"
            "  show_id bigint REFERENCES catalog.show(id))"
        )
    )
    stuck = _next_id()
    session.add(cm.Show(id=stuck, name="Referenced By Staging", tmdb_id=None))
    await session.flush()
    await session.execute(
        text("INSERT INTO import_ne.show_resolution (show_id) VALUES (:show_id)"),
        {"show_id": stuck},
    )
    await session.commit()

    try:
        assert await run(_parse_args(["retire"])) == 1
    finally:
        await session.execute(text("DROP TABLE import_ne.show_resolution"))
        await session.commit()


async def _retire_with_no_floor(db, **kwargs):
    """`retire_orphans` with the ingest floor lowered — three shows, not 150,000."""
    from tvbf.tmdb.orphan_retire import retire_orphans

    return await retire_orphans(db, **{**kwargs, "min_ingested": 0})
