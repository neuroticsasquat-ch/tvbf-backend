"""CLI-level tests for `python -m tvbf.jobs.weekly_recommendations`.

These exercise `run()` rather than `main()`, for the reason
`test_reconcile_cli.py` gives: `main`'s `asyncio.run` would rebuild the event
loop under the shared engine's pooled connections.

What matters here is the **split** — stdout is the payload and stderr is the
report — plus the two ways the run refuses, because a dry run that prints a
plausible payload for the wrong user, or under a guessed model id, is worse than
one that exits 1 — plus, since NEU-1111, the deadman pings the schedule hangs off
the whole-pass invocation.
"""

import json
import uuid

import httpx
import pytest
import respx

from tvbf.app.models import UserShowWatch
from tvbf.catalog.models import Show
from tvbf.config import get_settings
from tvbf.jobs import weekly_recommendations
from tvbf.jobs.weekly_recommendations import PassResult, _parse_args, run
from tvbf.recommendations.payload import GENERATION_FLOOR, INTERESTED_CAP, build_payload

MODEL = "deepseek-ai/DeepSeek-V4-Pro-0813"
FIRST_SHOW = 966_000
HEALTHCHECK = "https://hc.example.com/recommendations"


@pytest.fixture
def model(monkeypatch):
    """Pin `RECOMMENDATION_MODEL`, and leave no cached `Settings` behind.

    `get_settings` is `lru_cache`d, so a test that sets the env without clearing
    it either reads a stale value or hands one to whatever runs next
    (`tests/unit/catalog/test_images.py`).
    """
    monkeypatch.setenv("RECOMMENDATION_MODEL", MODEL)
    get_settings.cache_clear()
    yield MODEL
    get_settings.cache_clear()


@pytest.fixture
def no_model(monkeypatch):
    """The unset case, which is `config.py`'s deliberate default."""
    monkeypatch.delenv("RECOMMENDATION_MODEL", raising=False)
    get_settings.cache_clear()
    yield
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
        assert set(document) == {
            "columns",
            "exclude_columns",
            "liked",
            "not_liked",
            "interested",
            # Written whether or not it has entries, like every tier group: a
            # shape that varies with the data is one the model has to infer.
            "exclude",
        }
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

    async def test_the_report_says_what_the_interested_cap_dropped(
        self, session, make_user, shows, model, caplog
    ):
        """A tier at exactly 50 rows reads the same whether the user bookmarked
        50 shows or 300, so the rows alone cannot check the cap."""
        user = await make_user()
        await _track(session, user.id, shows)

        with caplog.at_level("INFO"):
            assert await run(_args(user.id)) == 0

        assert f"of {GENERATION_FLOOR} before the {INTERESTED_CAP}-row cap" in caplog.text

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


class TestTheDeadman:
    """The healthchecks.io pings the schedule feeds (NEU-1111).

    Coolify notifies when the task *fails*; only the deadman catches the task
    never running at all, and at a weekly cadence that failure is invisible for
    seven days. So every exit path is pinned here, including the two that
    deliberately stay silent.
    """

    @pytest.fixture
    def authenticated(self, monkeypatch):
        """A deployment that can authenticate. Says nothing about the deadman.

        Every environment fixture here clears the cache on the way *out* as well
        as in, for the reason `model` gives: `get_settings` is `lru_cache`d, so a
        test that leaves one behind hands it to whatever runs next. Cleanup in a
        test body would not survive the first failing assert above it.
        """
        monkeypatch.setenv("RECOMMENDATION_MODEL", MODEL)
        monkeypatch.setenv("DEEPINFRA_API_KEY", "key")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    @pytest.fixture
    def deadman(self, monkeypatch):
        """`HEALTHCHECK_RECOMMENDATIONS_URL` pointed at `HEALTHCHECK`."""
        monkeypatch.setenv("HEALTHCHECK_RECOMMENDATIONS_URL", HEALTHCHECK)
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    @pytest.fixture
    def no_deadman(self, monkeypatch):
        """The unset case, which is `config.py`'s default and what local runs want."""
        monkeypatch.delenv("HEALTHCHECK_RECOMMENDATIONS_URL", raising=False)
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    @pytest.fixture
    def configured(self, authenticated, deadman):
        """The whole scheduled-task environment: authenticates, and has a check."""

    @pytest.fixture
    def pass_result(self, monkeypatch):
        """Replace the pass body with one returning whatever this test needs.

        What `run_pass` does is covered by `test_weekly_recommendations_pass.py`;
        what is under test here is the exit code and the pings hung off it.
        """

        def _install(result):
            async def _fake(settings, *, user_id=None):
                if isinstance(result, Exception):
                    raise result
                return result

            monkeypatch.setattr(weekly_recommendations, "run_pass_if_free", _fake)

        return _install

    @respx.mock
    async def test_a_successful_pass_pings_start_then_success(self, configured, pass_result):
        pass_result(PassResult(succeeded=3))
        start = respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
        success = respx.post(HEALTHCHECK).mock(return_value=httpx.Response(200))

        assert await run(_parse_args([])) == 0
        assert start.called
        assert success.called

    @respx.mock
    async def test_a_pass_with_a_failed_user_pings_fail(self, configured, pass_result):
        """Exit 1 and /fail are the same claim told to two systems: Coolify sees
        the exit code, healthchecks.io sees the ping."""
        pass_result(PassResult(succeeded=1, failed=1))
        respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
        fail = respx.post(f"{HEALTHCHECK}/fail").mock(return_value=httpx.Response(200))

        assert await run(_parse_args([])) == 1
        assert fail.called

    @respx.mock
    async def test_a_crash_outside_a_users_turn_pings_fail(self, configured, pass_result, caplog):
        """Per-user failures leave a `failed` set behind; a crash outside one
        leaves nothing in the database to speak for it."""
        pass_result(RuntimeError("the lock query blew up"))
        respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
        fail = respx.post(f"{HEALTHCHECK}/fail").mock(return_value=httpx.Response(200))

        with caplog.at_level("ERROR"):
            assert await run(_parse_args([])) == 1

        assert fail.called
        assert "crashed" in caplog.text

    @respx.mock
    async def test_the_lock_being_held_pings_nothing_beyond_start(self, configured, pass_result):
        """The pass that *does* hold the lock pings nothing itself, so a success
        ping here would report an outcome this process never learns. Silence
        leaves the check started, the grace period expires, and someone looks."""
        pass_result(None)
        respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))

        assert await run(_parse_args([])) == 0
        assert len(respx.calls) == 1, "the skip path pinged something beyond /start"

    @respx.mock
    async def test_an_unconfigured_deployment_pings_fail(self, deadman, no_model):
        """A typo'd env var is a broken deployment, not a quiet week."""
        respx.post(f"{HEALTHCHECK}/start").mock(return_value=httpx.Response(200))
        fail = respx.post(f"{HEALTHCHECK}/fail").mock(return_value=httpx.Response(200))

        assert await run(_parse_args([])) == 1
        assert fail.called

    @respx.mock
    async def test_a_run_narrowed_to_one_user_pings_nothing(self, configured, pass_result):
        """`--user` is a hand-run: feeding the check with it would silence the
        deadman for the week on the strength of one account being covered."""
        pass_result(PassResult(succeeded=1))

        assert await run(_parse_args(["--user", str(uuid.uuid4())])) == 0
        assert not respx.calls, "a narrowed run fed the weekly deadman"

    @respx.mock
    async def test_an_unset_url_attempts_no_request(self, authenticated, no_deadman, pass_result):
        """Which is what local runs and the test suite want."""
        pass_result(PassResult(succeeded=1))

        assert await run(_parse_args([])) == 0
        assert not respx.calls
