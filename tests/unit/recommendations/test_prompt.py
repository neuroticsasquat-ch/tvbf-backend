"""The instruction and the parser (NEU-1109).

Pure — the request half is a string and the response half is a dict, so nothing
here needs a database or a provider. What the *recorded* responses do when they
reach this parser is pinned in `test_recorded_responses.py`, which is the half
that cannot be written by hand.
"""

import pytest

from tests.fixtures import recommendations as recorded
from tvbf.llm.client import _to_wire
from tvbf.llm.types import LLMResponseInvalid, Prompt
from tvbf.recommendations.prompt import (
    INSTRUCTION,
    RECOMMENDATION_COUNT,
    build_prompt,
    describe_dropped,
    parse_suggestions,
)


def _response(*entries) -> dict:
    return {"recommendations": list(entries)}


def _entry(**overrides) -> dict:
    entry = {"title": "Shōgun", "release_year": 2024, "reason": "You like historical drama."}
    entry.update(overrides)
    return entry


class TestTheInstruction:
    def test_it_survives_the_clients_json_guard(self):
        """`client._to_wire` refuses an instruction without the literal
        lower-case word — knowingly stricter than the provider measured on
        2026-08-15 (NEU-1100), and raised before anything is sent. A wording
        change that drops the word costs a user their week, so the guard is
        exercised against the real instruction rather than trusted."""
        wire = _to_wire("some/model", build_prompt("{}"))

        assert wire["messages"][0]["content"] == INSTRUCTION
        assert wire["response_format"] == {"type": "json_object"}

    def test_it_asks_for_the_count_the_spec_names(self):
        """§7's 25-asked-for against 12-displayed is what absorbs resolution
        failures and the never-recommend filter, so the number in the prose and
        the constant the pass logs have to be the same one."""
        assert str(RECOMMENDATION_COUNT) in INSTRUCTION

    def test_it_states_the_exclusion_rule_the_pass_also_enforces(self):
        """Belt-and-braces (§8): the instruction is the request and the
        post-resolution filter is the guarantee. Neither replaces the other."""
        assert "Never recommend a series that appears" in INSTRUCTION


class TestBuildPrompt:
    def test_the_user_message_is_the_payloads_own_bytes(self):
        """Not a re-serialization. The hash promises identical bytes mean
        identical output, and a second `json.dumps` with different separators
        would quietly break that promise."""
        payload_json = '{"columns":["title","year","pct","stars"],"liked":[["Dark",2017,100,5.0]]}'

        prompt = build_prompt(payload_json)

        assert isinstance(prompt, Prompt)
        assert prompt.user == payload_json


class TestParsingTheOutputContract:
    def test_a_well_formed_answer_keeps_the_models_order(self):
        first, second = _entry(title="A"), _entry(title="B")

        suggestions, dropped = parse_suggestions(_response(second, first))

        assert [s.title for s in suggestions] == ["B", "A"]
        assert dropped == []

    def test_a_recorded_answer_parses_to_twenty_five_suggestions(self):
        suggestions, dropped = parse_suggestions(
            {"recommendations": recorded.recommendations(recorded.CLEAN)}
        )

        assert len(suggestions) == RECOMMENDATION_COUNT
        assert dropped == []

    @pytest.mark.parametrize(
        "override",
        [
            pytest.param({"release_year": None}, id="no year"),
            pytest.param({"release_year": "2024"}, id="year as a string"),
            pytest.param({"release_year": True}, id="year as a bool, which is an int"),
            pytest.param({"title": ""}, id="blank title"),
            pytest.param({"reason": "   "}, id="whitespace-only reason"),
        ],
    )
    def test_an_entry_that_is_not_the_contract_is_dropped_rather_than_guessed_at(self, override):
        """§7: `release_year` is the only disambiguator resolution has, so an
        entry without a usable one is dropped. Title and reason go the same way
        — a card with neither is not renderable."""
        suggestions, dropped = parse_suggestions(_response(_entry(**override), _entry(title="Ok")))

        assert [s.title for s in suggestions] == ["Ok"]
        assert len(dropped) == 1

    def test_an_entry_that_is_not_an_object_at_all_is_dropped(self):
        suggestions, dropped = parse_suggestions(_response("Shōgun", _entry()))

        assert len(suggestions) == 1
        assert dropped == ["Shōgun"]

    def test_the_title_and_reason_come_back_stripped(self):
        suggestions, _ = parse_suggestions(_response(_entry(title="  Dark  ", reason=" Good.\n")))

        assert (suggestions[0].title, suggestions[0].reason) == ("Dark", "Good.")

    def test_a_response_that_is_not_the_contract_is_invalid_rather_than_empty(self):
        """`LLMResponseInvalid` and not an empty list, so the pass's single
        retry covers it: a body missing the key entirely is "a response arrived
        and could not be believed", the same verdict the client reaches for one
        that did not decode."""
        with pytest.raises(LLMResponseInvalid):
            parse_suggestions({"suggestions": []})

    def test_a_recommendations_key_that_is_not_a_list_is_invalid(self):
        with pytest.raises(LLMResponseInvalid):
            parse_suggestions({"recommendations": {"title": "Dark"}})

    def test_an_empty_list_is_a_believable_answer_with_nothing_in_it(self):
        """Distinct from the case above: the model honoured the shape and
        recommended nothing, which the pass records as `no_matches` rather than
        retrying."""
        assert parse_suggestions(_response()) == ([], [])


class TestDescribingWhatWasDropped:
    def test_it_names_the_titles_so_a_log_line_can_be_acted_on(self):
        _, dropped = parse_suggestions(
            _response(_entry(title="Dark", release_year=None), _entry(title="", release_year=None))
        )

        assert describe_dropped(dropped) == "Dark, <no title>"

    def test_an_entry_that_is_not_an_object_says_so(self):
        assert describe_dropped([42]) == "<not an object>"
