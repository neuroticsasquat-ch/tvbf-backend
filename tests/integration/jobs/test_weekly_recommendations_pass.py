"""The weekly pass and its failure semantics (NEU-1109).

DB-backed because almost everything the pass decides is decided against rows —
the regeneration gate reads the current set, resolution folds in Postgres, and
the four statuses are the only place a run's outcome is recorded. `respx`
throughout: **no test ever calls DeepInfra.**

These exercise `run()` rather than `main()`, for the reason
`test_reconcile_cli.py` gives: `main`'s `asyncio.run` would rebuild the event
loop under the shared engine's pooled connections.

The seeded catalog is deliberately its own, per project spec §12's known
constraint — `catalog` is sparsely populated locally while the ingest runs, and
a pass test that passes only against a full mirror is one that fails for the
next person.
"""

import json
from datetime import date

import httpx
import pytest
import respx
from sqlalchemy import func, select

from tvbf.app.models import (
    MATCHED_VIA_AKA,
    SET_STATUS_FAILED,
    SET_STATUS_INSUFFICIENT_HISTORY,
    SET_STATUS_NO_MATCHES,
    SET_STATUS_SUCCEEDED,
    UserRecommendation,
    UserRecommendationSet,
    UserShowWatch,
)
from tvbf.catalog.models import Show, ShowAka
from tvbf.config import get_settings
from tvbf.db import engine
from tvbf.jobs.weekly_recommendations import (
    ADVISORY_LOCK_KEY,
    CONSECUTIVE_FAILURE_LIMIT,
    _parse_args,
    run,
)
from tvbf.recommendations.payload import GENERATION_FLOOR, PROMPT_VERSION

COMPLETIONS = "https://api.deepinfra.com/v1/openai/chat/completions"
MODEL = "deepseek-ai/DeepSeek-V4-Pro-0813"

TRACKED = 972_000
"""The first of the shows the user already has, which the payload excludes."""

CANDIDATE = 973_000
"""The first of the shows nothing tracks — what a recommendation may resolve to."""


@pytest.fixture
def provider(monkeypatch):
    """A configured provider, and no cached `Settings` left behind.

    `get_settings` is `lru_cache`d, so a test that sets the env without clearing
    it either reads a stale value or hands one to whatever runs next.
    """
    monkeypatch.setenv("RECOMMENDATION_MODEL", MODEL)
    monkeypatch.setenv("DEEPINFRA_API_KEY", "test-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def catalog(session):
    """Ten shows the user tracks, and three they do not.

    Ten because the weighted floor is `(2 x liked) + interested >= 10` and a
    tracked show with nothing watched is INTERESTED, worth one each.
    """
    session.add_all(
        [Show(id=TRACKED + n, name=f"Tracked Show {n}", status="Ended") for n in range(10)]
    )
    session.add_all(
        [
            Show(id=CANDIDATE, name="Dark", first_air_date=date(2017, 12, 1), popularity=90.0),
            Show(
                id=CANDIDATE + 1, name="Shōgun", first_air_date=date(2024, 2, 27), popularity=80.0
            ),
            # Resolvable only through its English AKA, which is what pins
            # `matched_via` to something other than the default.
            Show(
                id=CANDIDATE + 2,
                name="La casa de papel",
                first_air_date=date(2017, 5, 2),
                popularity=70.0,
            ),
        ]
    )
    await session.flush()
    session.add(ShowAka(show_id=CANDIDATE + 2, title="Money Heist"))
    await session.commit()


@pytest.fixture
async def user(session, make_user, catalog):
    """A user over the floor: ten tracked shows, nothing watched."""
    account = await make_user()
    session.add_all([UserShowWatch(user_id=account.id, show_id=TRACKED + n) for n in range(10)])
    await session.commit()
    return account


def _recommendation(title: str, year: int, reason: str = "You will like it.") -> dict:
    return {"title": title, "release_year": year, "reason": reason}


def _envelope(*entries: dict, prompt_tokens: int = 6_748, completion_tokens: int = 1_100) -> dict:
    """A chat-completions body in the shape the recordings have."""
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"recommendations": list(entries)}),
                },
            }
        ],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def _mock(*responses: httpx.Response):
    return respx.post(COMPLETIONS).mock(side_effect=list(responses))


def _answer(*entries: dict) -> httpx.Response:
    return httpx.Response(200, json=_envelope(*entries))


async def _sets(session, user_id) -> list[UserRecommendationSet]:
    return list(
        (
            await session.execute(
                select(UserRecommendationSet)
                .where(UserRecommendationSet.user_id == user_id)
                .order_by(UserRecommendationSet.generated_at, UserRecommendationSet.id)
            )
        )
        .scalars()
        .all()
    )


async def _only_set(session, user_id) -> UserRecommendationSet:
    sets = await _sets(session, user_id)
    assert len(sets) == 1
    return sets[0]


async def _rows(session, set_id) -> list[UserRecommendation]:
    return list(
        (
            await session.execute(
                select(UserRecommendation)
                .where(UserRecommendation.set_id == set_id)
                .order_by(UserRecommendation.rank)
            )
        )
        .scalars()
        .all()
    )


class TestAHappyPass:
    @respx.mock
    async def test_it_stores_the_resolved_recommendations_in_the_models_order(
        self, session, user, provider
    ):
        _mock(_answer(_recommendation("Shōgun", 2024), _recommendation("Dark", 2017)))

        assert await run(_parse_args([])) == 0

        stored = await _only_set(session, user.id)
        assert stored.status == SET_STATUS_SUCCEEDED
        assert [row.show_id for row in await _rows(session, stored.id)] == [
            CANDIDATE + 1,
            CANDIDATE,
        ]

    @respx.mock
    async def test_it_records_what_it_was_told_and_what_it_cost(self, session, user, provider):
        """`compiled_payload` + `raw_response` are how a bad recommendation gets
        diagnosed, and the token counts are the scaling instrument (§9)."""
        _mock(_answer(_recommendation("Dark", 2017)))

        assert await run(_parse_args([])) == 0

        stored = await _only_set(session, user.id)
        assert stored.model == MODEL
        assert stored.prompt_version == PROMPT_VERSION
        assert (stored.input_tokens, stored.output_tokens) == (6_748, 1_100)
        assert len(stored.compiled_payload["interested"]) == GENERATION_FLOOR
        assert stored.raw_response is not None
        assert stored.raw_response["recommendations"][0]["title"] == "Dark"

    @respx.mock
    async def test_a_title_that_only_an_aka_carries_still_resolves(self, session, user, provider):
        _mock(_answer(_recommendation("Money Heist", 2017)))

        assert await run(_parse_args([])) == 0

        rows = await _rows(session, (await _only_set(session, user.id)).id)
        assert [(row.show_id, row.matched_via) for row in rows] == [
            (CANDIDATE + 2, MATCHED_VIA_AKA)
        ]

    @respx.mock
    async def test_unresolvable_titles_are_dropped_rather_than_failing_the_run(
        self, session, user, provider
    ):
        """§10.1: 25 titles returning 19 matches is a success with 19 rows —
        exactly as NEU-1043 treats unmatched shows as expected output."""
        _mock(
            _answer(
                _recommendation("A Show That Is Not In The Mirror", 1999),
                _recommendation("Dark", 2017),
            )
        )

        assert await run(_parse_args([])) == 0

        stored = await _only_set(session, user.id)
        assert stored.status == SET_STATUS_SUCCEEDED
        assert len(await _rows(session, stored.id)) == 1

    @respx.mock
    async def test_a_show_the_user_already_has_is_never_recommended_back(
        self, session, user, provider
    ):
        """The instruction states the rule and this filter guarantees it (§8).
        The taste payload *is* the exclusion list, so nothing queries twice."""
        _mock(_answer(_recommendation("Tracked Show 0", 2020), _recommendation("Dark", 2017)))

        assert await run(_parse_args([])) == 0

        rows = await _rows(session, (await _only_set(session, user.id)).id)
        assert [row.show_id for row in rows] == [CANDIDATE]

    @respx.mock
    async def test_two_titles_naming_one_show_are_stored_once(self, session, user, provider):
        """`uq_user_recommendation_set_rank` would not catch this — a set holding
        one show twice is two cards of the same thing in a grid of twelve."""
        _mock(_answer(_recommendation("Dark", 2017), _recommendation("Dark", 2018)))

        assert await run(_parse_args([])) == 0

        rows = await _rows(session, (await _only_set(session, user.id)).id)
        assert [(row.rank, row.show_id) for row in rows] == [(1, CANDIDATE)]


class TestWhenNoCallIsWorthMaking:
    @respx.mock
    async def test_an_unchanged_user_is_skipped_without_a_call(self, session, user, provider):
        """§9.1: the payload *is* the model's entire input, so identical bytes
        mean identical output. A user who changed nothing must not see their
        recommendations churn week to week."""
        route = _mock(_answer(_recommendation("Dark", 2017)))

        assert await run(_parse_args([])) == 0
        assert await run(_parse_args([])) == 0

        assert route.call_count == 1
        assert len(await _sets(session, user.id)) == 1

    @respx.mock
    async def test_a_changed_payload_supersedes_the_previous_set_rather_than_replacing_it(
        self, session, user, provider
    ):
        """The previous set simply stops being the newest — nothing is deleted
        ahead of a write that might fail (§9)."""
        _mock(_answer(_recommendation("Dark", 2017)), _answer(_recommendation("Shōgun", 2024)))

        assert await run(_parse_args([])) == 0
        session.add(UserShowWatch(user_id=user.id, show_id=CANDIDATE))
        await session.commit()
        assert await run(_parse_args([])) == 0

        sets = await _sets(session, user.id)
        assert [s.status for s in sets] == [SET_STATUS_SUCCEEDED, SET_STATUS_SUCCEEDED]
        assert sets[0].payload_hash != sets[1].payload_hash

    @respx.mock
    async def test_a_user_below_the_floor_is_recorded_and_never_called_for(
        self, session, make_user, catalog, provider
    ):
        """§5.4: a row with the hash and no recommendation rows, so the user is
        skipped at zero model cost until their payload changes."""
        route = _mock(_answer(_recommendation("Dark", 2017)))
        account = await make_user()
        session.add(UserShowWatch(user_id=account.id, show_id=TRACKED))
        await session.commit()

        assert await run(_parse_args([])) == 0

        stored = await _only_set(session, account.id)
        assert stored.status == SET_STATUS_INSUFFICIENT_HISTORY
        assert await _rows(session, stored.id) == []
        assert route.call_count == 0

    @respx.mock
    async def test_a_pass_can_be_narrowed_to_one_user(self, session, user, make_user, provider):
        """`--user` is the debugging affordance and the seam NEU-1110's admin
        trigger is written against."""
        _mock(_answer(_recommendation("Dark", 2017)))
        other = await make_user(email="other@example.com")

        assert await run(_parse_args(["--user", str(user.id)])) == 0

        assert len(await _sets(session, user.id)) == 1
        assert await _sets(session, other.id) == []


class TestWhenTheModelDisappoints:
    @respx.mock
    async def test_nothing_resolving_is_no_matches_so_last_weeks_set_stands(
        self, session, user, provider
    ):
        """Deliberately not `succeeded`: reads take the newest succeeded set, so
        this leaves last week's recommendations standing rather than silently
        emptying the section, and makes a systematic resolution break visible
        rather than looking like a quiet week (§10.1)."""
        _mock(_answer(_recommendation("Nothing In The Mirror", 1999)))

        assert await run(_parse_args([])) == 0

        stored = await _only_set(session, user.id)
        assert stored.status == SET_STATUS_NO_MATCHES
        assert stored.raw_response is not None

    @respx.mock
    async def test_an_unbelievable_answer_is_retried_exactly_once(self, session, user, provider):
        """Malformed model JSON is frequently a one-off and a second call costs
        a fraction of a cent (§10.1)."""
        route = _mock(
            httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]}),
            _answer(_recommendation("Dark", 2017)),
        )

        assert await run(_parse_args([])) == 0

        assert route.call_count == 2
        assert (await _only_set(session, user.id)).status == SET_STATUS_SUCCEEDED

    @respx.mock
    async def test_a_well_formed_body_that_is_not_the_output_contract_is_retried_too(
        self, session, user, provider
    ):
        """The same verdict reached one layer later. A body that decodes to an
        object with no `recommendations` list is still "a response arrived and
        could not be believed", so the retry has to cover it — which is why the
        parse sits inside the retried block rather than after it."""
        route = _mock(
            httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps({"suggestions": []})}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            ),
            _answer(_recommendation("Dark", 2017)),
        )

        assert await run(_parse_args([])) == 0

        assert route.call_count == 2
        assert (await _only_set(session, user.id)).status == SET_STATUS_SUCCEEDED

    @respx.mock
    async def test_a_second_unbelievable_answer_fails_that_user(self, session, user, provider):
        unbelievable = httpx.Response(200, json={"choices": [{"message": {"content": "{"}}]})
        route = _mock(unbelievable, unbelievable)

        assert await run(_parse_args([])) == 1

        assert route.call_count == 2
        stored = await _only_set(session, user.id)
        assert stored.status == SET_STATUS_FAILED
        assert stored.raw_response is None

    @respx.mock
    async def test_a_provider_failure_is_not_retried_by_the_job(self, session, user, provider):
        """The client has already walked its backoff curve, so asking again
        immediately buys the same failure (`llm/types`). One attempt reaches
        `respx` because the retry policy's own retries are what were spent."""
        route = _mock(httpx.Response(400, json={"error": "no such model"}))

        assert await run(_parse_args([])) == 1

        assert route.call_count == 1
        assert (await _only_set(session, user.id)).status == SET_STATUS_FAILED


class TestIsolationAndGivingUp:
    @respx.mock
    async def test_one_users_failure_does_not_cost_the_others_their_week(
        self, session, user, make_user, catalog, provider
    ):
        """Five users must not lose recommendations because the first hit a 429
        (§10.1). The exit code is still 1 — one failure is 20-33% of the user
        base at this scale."""
        second = await make_user(email="second@example.com")
        session.add_all([UserShowWatch(user_id=second.id, show_id=TRACKED + n) for n in range(10)])
        await session.commit()
        # A 400 rather than a 5xx: `retry.retry_for_status` walks its curve on a
        # 5xx, so a transient status would consume an unpredictable number of
        # mocked responses and make the assertion about the *second* user's
        # answer depend on the retry policy rather than on the isolation.
        _mock(
            httpx.Response(400, json={"error": "no such model"}),
            _answer(_recommendation("Dark", 2017)),
        )

        assert await run(_parse_args([])) == 1

        assert (await _only_set(session, user.id)).status == SET_STATUS_FAILED
        assert (await _only_set(session, second.id)).status == SET_STATUS_SUCCEEDED

    @respx.mock
    async def test_it_abandons_the_rest_after_consecutive_failures(
        self, session, make_user, catalog, provider
    ):
        """A provider that has failed three users running is not going to serve
        the fourth, and the calls are what cost money."""
        accounts = []
        for n in range(CONSECUTIVE_FAILURE_LIMIT + 1):
            account = await make_user(email=f"user{n}@example.com")
            session.add_all(
                [UserShowWatch(user_id=account.id, show_id=TRACKED + s) for s in range(10)]
            )
            accounts.append(account)
        await session.commit()
        _mock(*[httpx.Response(400, json={"error": "no such model"})] * (len(accounts) + 1))

        assert await run(_parse_args([])) == 1

        written = (
            await session.execute(select(func.count()).select_from(UserRecommendationSet))
        ).scalar_one()
        assert written == CONSECUTIVE_FAILURE_LIMIT
        assert await _sets(session, accounts[-1].id) == []


class TestTheAdvisoryLock:
    @respx.mock
    async def test_a_second_pass_finding_the_lock_held_exits_zero_without_working(
        self, session, user, provider
    ):
        """A concurrent pass is not an error — failing would have Coolify notify
        on a benign condition. Without the guard both processes read the same
        stale hash, both find the user dirty, and both spend a call (§10)."""
        route = _mock(_answer(_recommendation("Dark", 2017)))

        async with engine.connect() as holder:
            held = (
                await holder.execute(select(func.pg_try_advisory_lock(ADVISORY_LOCK_KEY)))
            ).scalar_one()
            assert held
            try:
                assert await run(_parse_args([])) == 0
            finally:
                await holder.execute(select(func.pg_advisory_unlock(ADVISORY_LOCK_KEY)))

        assert route.call_count == 0
        assert await _sets(session, user.id) == []

    @respx.mock
    async def test_the_lock_is_released_when_the_pass_finishes(self, session, user, provider):
        """Two runs back to back, which only works if the first let go."""
        _mock(_answer(_recommendation("Dark", 2017)), _answer(_recommendation("Shōgun", 2024)))

        assert await run(_parse_args([])) == 0
        session.add(UserShowWatch(user_id=user.id, show_id=CANDIDATE))
        await session.commit()
        assert await run(_parse_args([])) == 0

        assert len(await _sets(session, user.id)) == 2


class TestHowItRefusesToStart:
    async def test_an_unconfigured_provider_exits_one_before_compiling_anything(
        self, session, user, monkeypatch
    ):
        """A pass that compiles five payloads and then discovers it cannot
        authenticate has spent five users' turns learning it."""
        monkeypatch.delenv("RECOMMENDATION_MODEL", raising=False)
        monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
        get_settings.cache_clear()
        try:
            assert await run(_parse_args([])) == 1
        finally:
            get_settings.cache_clear()

        assert await _sets(session, user.id) == []
