"""CLI-level tests for `python -m tvbf.jobs.reconcile` (NEU-1030).

These exercise `run()` rather than `main()`:
`main` is a three-line wrapper whose `asyncio.run` would rebuild the event loop
under the shared engine's pooled connections. What matters here is the **exit
code**, because that is the contract the cutover gate reads.

They seed nothing — each captures the database as it stands and diffs against
that, so the assertions hold whatever rows happen to exist.
"""

import json

import pytest

from tvbf.jobs.reconcile import _parse_args, main, run


async def _capture_to(path, capsys) -> dict:
    assert await run(_parse_args(["capture"])) == 0
    blob = capsys.readouterr().out
    path.write_text(blob)
    return json.loads(blob)


@pytest.mark.asyncio
async def test_capture_writes_only_the_artifact_to_stdout(session, tmp_path, capsys):
    snapshot = await _capture_to(tmp_path / "baseline.json", capsys)

    # Nothing but JSON on stdout — logs go to stderr, so the caller can redirect
    # straight into a file over `ssh docker exec`.
    assert snapshot["artifact_version"] == 1
    assert snapshot["spine"] == "catalog"
    assert set(snapshot) == {"artifact_version", "spine", "totals", "users"}


@pytest.mark.asyncio
async def test_capture_is_byte_identical_across_runs(session, tmp_path, capsys):
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    await _capture_to(first, capsys)
    await _capture_to(second, capsys)

    assert first.read_text() == second.read_text()
    assert first.read_text().endswith("\n")


@pytest.mark.asyncio
async def test_verify_exits_zero_when_nothing_moved(session, tmp_path, capsys):
    baseline = tmp_path / "baseline.json"
    await _capture_to(baseline, capsys)

    assert await run(_parse_args(["verify", "--baseline", str(baseline)])) == 0


@pytest.mark.asyncio
async def test_verify_exits_one_when_the_baseline_says_a_row_is_missing(
    session, tmp_path, capsys, caplog
):
    baseline = tmp_path / "baseline.json"
    snapshot = await _capture_to(baseline, capsys)

    # A user the database does not have, holding rows it therefore cannot show.
    snapshot["users"].append(
        {
            "user_id": "00000000-0000-0000-0000-0000000000ff",
            "totals": {
                "tracked_shows": 1,
                "episode_watches": 3,
                "show_ratings": 0,
                "episode_ratings": 0,
                "activity_events": 0,
            },
            "shows": [
                {
                    "show_id": 1,
                    "tracked_shows": 1,
                    "episode_watches": 3,
                    "show_ratings": 0,
                    "episode_ratings": 0,
                    "activity_events": 0,
                }
            ],
        }
    )
    baseline.write_text(json.dumps(snapshot))

    assert await run(_parse_args(["verify", "--baseline", str(baseline)])) == 1
    assert "LOST 3 episode_watches" in caplog.text


@pytest.mark.asyncio
async def test_verify_without_a_baseline_path_fails_rather_than_passing_vacuously(session):
    assert await run(_parse_args(["verify"])) == 1


def test_an_unknown_spine_is_refused_by_the_parser():
    """The registry is the whitelist; argparse enforces it before any SQL runs."""
    with pytest.raises(SystemExit):
        _parse_args(["capture", "--spine", "tvmaze; DROP TABLE app.user --"])


def test_main_is_a_thin_wrapper_over_run():
    """Covers the real entrypoint's argument plumbing without a second event loop."""
    assert main(["verify"]) == 1
