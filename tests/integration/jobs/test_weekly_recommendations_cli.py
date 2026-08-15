"""CLI-level tests for `python -m tvbf.jobs.weekly_recommendations --dry-run`.

These exercise `run()` rather than `main()`, for the reason
`test_reconcile_cli.py` gives: `main`'s `asyncio.run` would rebuild the event
loop under the shared engine's pooled connections.

What matters here is the **split** — stdout is the payload and stderr is the
report — plus the two ways the run refuses, because a dry run that prints a
plausible payload for the wrong user, or under a guessed model id, is worse than
one that exits 1.
"""

import json
import os
import uuid

import pytest

from tvbf.app.models import UserShowWatch
from tvbf.catalog.models import Show
from tvbf.config import get_settings
from tvbf.jobs.weekly_recommendations import _parse_args, run
from tvbf.recommendations.payload import GENERATION_FLOOR, build_payload

MODEL = "deepseek-ai/DeepSeek-V4-Pro-0813"
FIRST_SHOW = 966_000


@pytest.fixture
def model():
    """Pin `RECOMMENDATION_MODEL`, and leave no cached `Settings` behind.

    Written against `os.environ` rather than `monkeypatch` on purpose: this
    fixture is used alongside the session-scoped `session` fixture, and CLAUDE.md
    records that mixing `monkeypatch` into that teardown order is how the admin
    tests broke once already.
    """
    previous = os.environ.get("RECOMMENDATION_MODEL")
    os.environ["RECOMMENDATION_MODEL"] = MODEL
    get_settings.cache_clear()
    yield MODEL
    if previous is None:
        os.environ.pop("RECOMMENDATION_MODEL", None)
    else:
        os.environ["RECOMMENDATION_MODEL"] = previous
    get_settings.cache_clear()


@pytest.fixture
def no_model():
    """The unset case, which is `config.py`'s deliberate default."""
    previous = os.environ.pop("RECOMMENDATION_MODEL", None)
    get_settings.cache_clear()
    yield
    if previous is not None:
        os.environ["RECOMMENDATION_MODEL"] = previous
    get_settings.cache_clear()


@pytest.fixture
async def shows(session):
    """Seeded and **committed**: `run` opens its own `SessionLocal`, so anything
    left in this session's transaction is invisible to the thing under test."""
    session.add_all(
        [
            Show(id=FIRST_SHOW + n, name=f"Dry Run Show {n}", status="Ended")
            for n in range(GENERATION_FLOOR)
        ]
    )
    await session.commit()
    return [FIRST_SHOW + n for n in range(GENERATION_FLOOR)]


async def _track(session, user_id, show_ids) -> None:
    session.add_all([UserShowWatch(user_id=user_id, show_id=sid) for sid in show_ids])
    await session.commit()


def _args(user_id):
    return _parse_args(["--dry-run", "--user", str(user_id)])


class TestWhatItPrints:
    async def test_stdout_carries_the_payload_and_nothing_else(
        self, session, make_user, shows, model, capsys
    ):
        user = await make_user()
        await _track(session, user.id, shows)

        assert await run(_args(user.id)) == 0

        document = json.loads(capsys.readouterr().out)
        assert set(document) == {"columns", "liked", "not_liked", "interested"}
        assert len(document["interested"]) == GENERATION_FLOOR

    async def test_stdout_is_the_exact_bytes_the_hash_covers(
        self, session, make_user, shows, model, capsys
    ):
        """Only the trailing newline separates the redirected file from the hash
        input — that newline is the terminal's, not the payload's."""
        user = await make_user()
        await _track(session, user.id, shows)

        assert await run(_args(user.id)) == 0

        printed = capsys.readouterr().out
        expected = await build_payload(session, user_id=user.id, model=MODEL)
        assert printed == expected.json + "\n"

    async def test_the_report_goes_to_stderr_so_a_redirect_keeps_the_artifact_clean(
        self, session, make_user, shows, model, capsys, caplog
    ):
        user = await make_user()
        await _track(session, user.id, shows)

        with caplog.at_level("INFO"):
            assert await run(_args(user.id)) == 0

        out = capsys.readouterr().out
        report = caplog.text
        assert "hash" not in out
        assert "tokens" not in out
        assert "payload hash" in report
        assert "tokens" in report
        assert f"{GENERATION_FLOOR} interested" in report

    async def test_an_account_below_the_floor_still_exits_zero_and_says_so(
        self, session, make_user, shows, model, capsys, caplog
    ):
        """The dry run answered the question; the answer is one of the things
        worth looking at, not a failure."""
        user = await make_user()
        await _track(session, user.id, shows[:1])

        with caplog.at_level("INFO"):
            assert await run(_args(user.id)) == 0

        assert "below the generation floor" in caplog.text
        assert json.loads(capsys.readouterr().out)["interested"]


class TestHowItRefuses:
    async def test_an_unknown_user_exits_one_and_prints_no_payload(
        self, session, model, capsys, caplog
    ):
        assert await run(_args(uuid.uuid4())) == 1

        assert capsys.readouterr().out == ""
        assert "no user with id" in caplog.text

    async def test_an_unset_model_exits_one_before_touching_the_database(
        self, session, make_user, shows, no_model, capsys, caplog
    ):
        """The model id is *in* the hash, so a payload compiled under a guessed
        one is a hash a real run will never match."""
        user = await make_user()
        await _track(session, user.id, shows)

        assert await run(_args(user.id)) == 1

        assert capsys.readouterr().out == ""
        assert "RECOMMENDATION_MODEL is unset" in caplog.text
