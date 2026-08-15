"""The dry run's pure halves: the token estimate and the argument contract.

The payload itself is pinned by `tests/*/recommendations/test_payload.py`; what
is only decidable here is what the CLI accepts and what the estimate claims.
"""

import pytest

from tvbf.jobs.weekly_recommendations import _parse_args, estimate_tokens

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

    def test_the_bare_invocation_is_refused_rather_than_treated_as_the_pass(self):
        """The weekly pass is M4. Exiting 0 having done nothing would read as
        "no user needed regenerating", which is a different claim."""
        with pytest.raises(SystemExit):
            _parse_args([])

    def test_a_user_that_is_not_a_uuid_is_refused_by_the_parser(self):
        with pytest.raises(SystemExit):
            _parse_args(["--dry-run", "--user", "tom@example.com"])

    def test_a_dry_run_with_a_user_parses(self):
        args = _parse_args(["--dry-run", "--user", USER])

        assert args.dry_run is True
        assert str(args.user) == USER
