"""The job's pure halves: the token estimate, the argument contract, the tally.

The payload itself is pinned by `tests/*/recommendations/test_payload.py` and the
pass by `tests/integration/jobs/test_weekly_recommendations_pass.py`; what is only
decidable here is what the CLI accepts, what the estimate claims, and how a run's
outcomes turn into an exit code.
"""

import pytest

from tvbf.app.models import (
    SET_STATUS_INSUFFICIENT_HISTORY,
    SET_STATUS_NO_MATCHES,
    SET_STATUS_SUCCEEDED,
)
from tvbf.jobs.weekly_recommendations import (
    PassResult,
    UserOutcome,
    _parse_args,
    estimate_tokens,
)

USER = "3f2a1c44-0000-4000-8000-00000000abcd"


class TestTheTokenEstimate:
    def test_it_reproduces_the_measurement_it_is_calibrated_on(self):
        """NEU-1100 measured the 522-row account at 17,825 bytes / 6,748 input
        tokens. The ratio is only worth carrying if it still answers that."""
        assert estimate_tokens("x" * 17_825) == pytest.approx(6_748, rel=0.01)

    def test_it_counts_utf_8_bytes_rather_than_characters(self):
        """A title in its own script is more bytes than characters, and
        `ensure_ascii=False` means the payload carries it as itself — so a
        character count would under-report exactly the payloads that cost most."""
        assert estimate_tokens("あ" * 100) > estimate_tokens("a" * 100)

    def test_an_empty_payload_costs_nothing(self):
        assert estimate_tokens("") == 0


class TestTheArgumentContract:
    def test_a_dry_run_needs_a_user(self):
        with pytest.raises(SystemExit):
            _parse_args(["--dry-run"])

    def test_the_bare_invocation_is_the_pass_over_everybody(self):
        """What the Coolify schedule runs. `--user` narrows it to one account —
        a debugging affordance, and the seam NEU-1110's admin trigger is
        written against."""
        args = _parse_args([])

        assert args.dry_run is False
        assert args.user is None

    def test_a_user_that_is_not_a_uuid_is_refused_by_the_parser(self):
        with pytest.raises(SystemExit):
            _parse_args(["--dry-run", "--user", "tom@example.com"])

    def test_a_dry_run_with_a_user_parses(self):
        args = _parse_args(["--dry-run", "--user", USER])

        assert args.dry_run is True
        assert str(args.user) == USER


class TestTheTally:
    def test_a_run_where_nothing_failed_exits_zero(self):
        """`insufficient_history` and `no_matches` are the pass working, not
        failures — only a user whose turn raised makes the process exit 1."""
        result = PassResult()
        result.record(UserOutcome(status=None))
        result.record(UserOutcome(status=SET_STATUS_INSUFFICIENT_HISTORY))
        result.record(UserOutcome(status=SET_STATUS_NO_MATCHES))
        result.record(UserOutcome(status=SET_STATUS_SUCCEEDED, unresolved=6))

        assert (result.skipped, result.insufficient, result.no_matches, result.succeeded) == (
            1,
            1,
            1,
            1,
        )
        assert result.unresolved == 6
        assert result.ok

    def test_one_failed_user_is_enough_to_exit_one(self):
        """At 3-5 accounts one failure is 20-33% of the user base, and the
        client has already walked its backoff curve."""
        result = PassResult(failed=1)

        assert not result.ok

    def test_an_abandoned_run_exits_one_even_though_it_reached_nobody_else(self):
        result = PassResult(aborted=True)

        assert not result.ok

    def test_a_status_a_turn_cannot_end_in_is_refused_rather_than_miscounted(self):
        """`failed` reaches `run_pass` as an exception and never as an outcome,
        so counting it here would put it in somebody else's column."""
        with pytest.raises(ValueError):
            PassResult().record(UserOutcome(status="failed"))
