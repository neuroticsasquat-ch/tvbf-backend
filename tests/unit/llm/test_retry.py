"""The retry policy's own judgement (NEU-1098), with no wire involved.

`test_client.py` covers which failures get retried; this covers *how long* the
waits are, which is the half that is silent when wrong — an under-retried call
does not surface as an error, it surfaces as a user who got no recommendations.
"""

import pytest

from tvbf.llm.retry import (
    DEFAULT_RETRY_POLICY,
    Retry,
    RetryPolicy,
    call_with_retry,
    retry_after_seconds,
    retry_for_status,
)


class TestTheDefaultPolicy:
    """The one value here that is a measurement rather than a judgement."""

    def test_the_timeout_clears_the_measured_latency_of_the_model_in_production(self):
        """NEU-1180: 60s did not, and the failure was silent.

        `ByteDance/Seed-2.0-mini` runs a 49.2s median and a 57.0s max on a
        522-row payload (`scripts/probe_deepinfra.py --model`, 2026-08-18).
        Under the old 60s ceiling both real production accounts exhausted the
        retry curve on timeouts and were recorded `failed` with no response
        body at all. Anything at or below the measured max puts that back.
        """
        assert DEFAULT_RETRY_POLICY.timeout > 57.0


class TestDelayFor:
    def test_it_backs_off_exponentially_within_the_cap(self):
        policy = RetryPolicy(initial_backoff=0.5, max_backoff=8.0, jitter=0.0)
        assert [policy.delay_for(n) for n in (1, 2, 3, 4, 5, 6)] == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]

    def test_jitter_only_ever_shortens_a_wait(self):
        """Shortened by up to `jitter` of itself, so concurrent callers do not
        retry in lockstep. Never lengthened — that would silently exceed the
        curve's cap."""
        policy = RetryPolicy(initial_backoff=1.0, jitter=0.25)
        assert policy.delay_for(1, rand=lambda: 1.0) == pytest.approx(0.75)
        assert policy.delay_for(1, rand=lambda: 0.0) == pytest.approx(1.0)

    def test_a_retry_after_wins_over_the_curve(self):
        policy = RetryPolicy(initial_backoff=0.5, jitter=0.0)
        assert policy.delay_for(1, 12.0) == 12.0

    def test_an_oversized_retry_after_is_clamped_rather_than_discarded(self):
        """Discarding it drops the wait back to the curve's first step, so a
        provider asking for two minutes gets asked again in half a second and
        429s every time — spending the whole retry budget inside a window it had
        already said was closed. Waiting less than asked is the one option
        guaranteed to fail."""
        policy = RetryPolicy(initial_backoff=0.5, jitter=0.0, max_retry_after=60.0)
        assert policy.delay_for(1, 600.0) == 60.0

    def test_a_nonsensical_retry_after_falls_back_to_the_curve(self):
        policy = RetryPolicy(initial_backoff=0.5, jitter=0.0)
        assert policy.delay_for(1, 0.0) == 0.5
        assert policy.delay_for(1, -5.0) == 0.5


class TestStatusVerdicts:
    @pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 599])
    def test_transient_statuses_are_retried(self, status):
        assert retry_for_status(status, None) is not None

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_everything_else_fails_identically_four_times(self, status):
        assert retry_for_status(status, None) is None

    def test_the_providers_own_retry_after_rides_along(self):
        assert retry_for_status(429, {"Retry-After": "30"}) == Retry(retry_after=30.0)

    def test_the_http_date_form_is_not_parsed(self):
        """It needs a clock and a timezone to mean anything, and falling back to
        the curve is the safe failure — a slightly wrong wait, never a wrong
        number of attempts."""
        assert retry_after_seconds({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None

    def test_a_missing_header_is_no_hint_at_all(self):
        assert retry_after_seconds(None) is None
        assert retry_after_seconds({}) is None


class TestCallWithRetry:
    async def test_it_stops_once_the_retries_run_out_and_reraises_the_original(self):
        attempts = 0

        async def _fail():
            nonlocal attempts
            attempts += 1
            raise RuntimeError("nope")

        async def _no_sleep(_seconds: float) -> None:
            return None

        with pytest.raises(RuntimeError, match="nope"):
            await call_with_retry(
                _fail,
                policy=RetryPolicy(max_retries=2),
                classify=lambda _exc: Retry(),
                sleep=_no_sleep,
            )
        # One attempt plus two retries. The original exception propagates
        # untouched — the client is what turns it into an `LLMError`, because that
        # is where the taxonomy lives.
        assert attempts == 3

    async def test_a_verdict_of_none_stops_immediately(self):
        attempts = 0

        async def _fail():
            nonlocal attempts
            attempts += 1
            raise RuntimeError("nope")

        with pytest.raises(RuntimeError):
            await call_with_retry(
                _fail, policy=RetryPolicy(max_retries=5), classify=lambda _exc: None
            )
        assert attempts == 1

    async def test_the_waits_follow_the_policy(self):
        waited: list[float] = []

        async def _record(seconds: float) -> None:
            waited.append(seconds)

        calls = 0

        async def _fail_twice():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError("nope")
            return "ok"

        result = await call_with_retry(
            _fail_twice,
            policy=RetryPolicy(max_retries=3, initial_backoff=0.5, jitter=0.0),
            classify=lambda _exc: Retry(),
            sleep=_record,
            rand=lambda: 0.0,
        )
        assert result == "ok"
        assert waited == [0.5, 1.0]
