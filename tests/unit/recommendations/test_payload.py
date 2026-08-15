"""The payload's pure halves: the hash's inputs and the generation floor.

Everything that needs a database — the row order, the tiers, the exclusion
union — is pinned in `tests/integration/recommendations/test_payload.py`,
because the fold the order depends on is only evaluated in Postgres.
"""

import pytest

from tvbf.recommendations.payload import (
    COLUMNS,
    GENERATION_FLOOR,
    INTERESTED_CAP,
    LIKED_WEIGHT,
    TastePayload,
    payload_hash,
    to_canonical_json,
)
from tvbf.recommendations.taste import TasteLabel


def _payload(*, liked: int = 0, interested: int = 0) -> TastePayload:
    return TastePayload(
        json="{}",
        hash="",
        liked_count=liked,
        interested_count=interested,
        excluded_show_ids=frozenset(),
    )


class TestTheFloor:
    def test_liked_alone_qualifies_at_five(self):
        assert not _payload(liked=4).meets_floor
        assert _payload(liked=5).meets_floor

    def test_interested_alone_qualifies_at_ten(self):
        assert not _payload(interested=9).meets_floor
        assert _payload(interested=10).meets_floor

    def test_the_mixed_middle_two_thresholds_would_refuse_qualifies(self):
        # 3 LIKED + 4 INTERESTED is under both endpoints taken separately and
        # over the weighted floor, which is the whole reason it is one expression.
        assert _payload(liked=3, interested=4).meets_floor

    def test_not_liked_contributes_nothing(self):
        # It is exclusion signal; there is no field for it here, and a payload of
        # nothing but NOT LIKED rows reads as zero.
        assert not _payload().meets_floor

    def test_the_weights_are_the_named_constants(self):
        assert LIKED_WEIGHT == 2
        assert GENERATION_FLOOR == 10
        assert INTERESTED_CAP == 50


class TestTheHash:
    def test_identical_inputs_hash_identically(self):
        args = {"prompt_version": "1", "model": "m", "canonical_json": "{}"}
        assert payload_hash(**args) == payload_hash(**args)

    @pytest.mark.parametrize(
        "changed",
        [
            {"prompt_version": "2"},
            {"model": "other"},
            {"canonical_json": '{"a":1}'},
        ],
    )
    def test_every_part_of_the_input_moves_the_hash(self, changed):
        base = {"prompt_version": "1", "model": "m", "canonical_json": "{}"}
        assert payload_hash(**base) != payload_hash(**{**base, **changed})

    def test_the_parts_cannot_run_together(self):
        # Bare concatenation would make these two the same input, and a silent
        # skip is the worst failure this gate has.
        assert payload_hash(prompt_version="1", model="a/b", canonical_json="{}") != payload_hash(
            prompt_version="1a", model="/b", canonical_json="{}"
        )


class TestTheCanonicalForm:
    def test_the_columns_are_the_spec_order(self):
        assert list(COLUMNS) == ["title", "year", "pct", "stars"]

    def test_it_is_the_shape_the_spec_writes(self):
        assert to_canonical_json(
            {
                TasteLabel.LIKED: [["Game of Thrones", 2011, 100, 4.5]],
                TasteLabel.NOT_LIKED: [["Emily in Paris", 2020, 12, None]],
                TasteLabel.INTERESTED: [["Dark", 2017, 0, None]],
            }
        ) == (
            '{"columns":["title","year","pct","stars"],'
            '"liked":[["Game of Thrones",2011,100,4.5]],'
            '"not_liked":[["Emily in Paris",2020,12,null]],'
            '"interested":[["Dark",2017,0,null]]}'
        )

    def test_an_empty_tier_is_still_written(self):
        # A shape that varies with the data is one the model has to infer and one
        # the hash would churn on.
        assert to_canonical_json({}) == (
            '{"columns":["title","year","pct","stars"],"liked":[],"not_liked":[],"interested":[]}'
        )

    def test_a_non_ascii_title_stays_itself(self):
        assert '"Shōgun"' in to_canonical_json({TasteLabel.LIKED: [["Shōgun", 2024, 100, None]]})
