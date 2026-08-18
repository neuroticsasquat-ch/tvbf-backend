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
from tvbf.recommendations.payload import PROMPT_VERSION
from tvbf.recommendations.prompt import (
    INSTRUCTION,
    RECOMMENDATION_COUNT,
    build_prompt,
    describe_dropped,
    parse_suggestions,
    quoted_candidate,
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
        assert "none of them may appear in your answer" in INSTRUCTION

    def test_it_claims_nothing_about_the_user_having_seen_an_excluded_series(self):
        """NEU-1178: a dismissal can name a show the user has never met, so both
        clauses that used to justify the ban with "they already have it" were
        false for it — and the second made the ban grammatically *derive* from
        the false premise. The ban itself is unchanged; what is gone is the
        reasoning a model could find inapplicable.
        """
        assert '"exclude" is a plain list of further series to leave out' in INSTRUCTION
        assert "is one this person must not be recommended" in INSTRUCTION
        assert "one this person already has" not in INSTRUCTION
        assert "further series they already have" not in INSTRUCTION

    def test_the_prompt_version_is_bumped_with_the_wording(self):
        """CLAUDE.md's rule: the constant versions the whole request/response
        contract and moves in the same commit as the prose, or the change is
        never evaluated against a real user (§9.1)."""
        assert PROMPT_VERSION == "5"

    def test_the_exclusion_rule_names_every_group_it_covers(self):
        """Including `exclude`, which is the group that made the rule followable:
        under versions 1 and 2 the ban reached shows the payload never mentioned,
        so the model was asked to avoid rows it could not see."""
        for group in ('"liked"', '"not_liked"', '"interested"', '"exclude"'):
            assert group in INSTRUCTION

    def test_it_says_what_to_do_instead_of_a_series_the_user_has(self):
        """Version 2 said only "never say that you are avoiding one", and the
        model answered by dropping the *redirect* rather than the seen show — 25
        of 25 titles already in its own input. Saying what to do instead is the
        half that was missing."""
        assert "give the next best one you have not used" in INSTRUCTION
        assert "do not offer it with a caveat" in INSTRUCTION

    def test_it_demands_a_bare_title_and_says_what_a_dressed_one_costs(self):
        """A production run stored 5 of 25 because the model wrote its reasoning
        into `title` while every entry stayed structurally valid, so nothing
        before resolution could object. Asking for "the title" was not enough —
        the instruction names the failure modes and the consequence."""
        assert "and nothing " in INSTRUCTION
        assert "no commentary" in INSTRUCTION
        assert "cannot be looked up" in INSTRUCTION

    def test_it_tells_the_model_not_to_narrate_the_exclusions(self):
        """The prose that broke the first run editorialised around shows the user
        had already seen ("though you've seen it"): the model wanted to explain
        what it was skipping and had nowhere to put it at the point of naming.

        The ban on narrating it survives; what changed in version 3 is that it no
        longer stands alone — see the test above for the instruction it needs
        beside it.
        """
        assert "drop it without comment" in INSTRUCTION
        assert "Do not name it, do not describe it" in INSTRUCTION


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


class TestRecoveringADressedTitle:
    """NEU-1173. The five strings below are verbatim model output, not authored
    examples — the same argument `tests/fixtures/recommendations/` rests on.
    Four are from the `PROMPT_VERSION` 3 run of 2026-08-17 16:32; the fifth is
    the version-1 answer recorded in `INSTRUCTION`'s docstring.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("The Americans' sibling 'The Spy'", "The Spy", id="possessive s'"),
            pytest.param("Halt and Catch Fire's 'The Company'", "The Company", id="possessive 's"),
            pytest.param(
                "The Leftovers' 'Manhunt: Unabomber'",
                "Manhunt: Unabomber",
                id="colon in the recommendation",
            ),
            pytest.param("Killing Eve's 'Bodyguard'", "Bodyguard", id="bare"),
            pytest.param(
                "Succession's corporate peer, 'Industry' (though you've seen it), try 'Billions'",
                "Billions",
                id="version 1, two quoted runs",
            ),
        ],
    )
    def test_the_recorded_dressed_titles_yield_the_recommendation(self, raw, expected):
        assert quoted_candidate(raw) == expected

    def test_the_pairing_runs_right_to_left_because_left_to_right_is_garbage(self):
        """Every observed dressed title carries an *odd* number of apostrophes,
        because the connective is a possessive. Pairing from the left recovers
        `" sibling "` here — and on the version-1 case it recovers `Industry`,
        the one show the model was explicitly declining because the user has it.
        The trailing run is the recommendation; the leading one never is."""
        assert quoted_candidate("The Americans' sibling 'The Spy'") == "The Spy"
        assert (
            quoted_candidate("Succession's corporate peer, 'Industry', try 'Billions'")
            == "Billions"
        )

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("Killing Eve's 'Bodyguard'", "Bodyguard", id="straight single"),
            pytest.param('Killing Eve: "Bodyguard"', "Bodyguard", id="straight double"),
            pytest.param("Killing Eve’s ‘Bodyguard’", "Bodyguard", id="curly single"),
            pytest.param("Killing Eve: “Bodyguard”", "Bodyguard", id="curly double"),
            pytest.param('Killing Eve\'s "Bodyguard"', "Bodyguard", id="mixed"),
        ],
    )
    def test_every_delimiter_pair_is_recognised(self, raw, expected):
        """A close-quote partners with its own open form, which is why the
        typographic pairs work at all: `’` is ambiguous between possessive and
        close-quote, `‘` unambiguously opens."""
        assert quoted_candidate(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("The Spy", id="no delimiter at all"),
            pytest.param(
                "The Americans' sibling The Spy",
                id="one unpartnered delimiter",
            ),
            pytest.param("Killing Eve ‘Bodyguard", id="an opener with nothing after it"),
            pytest.param("Killing Eve ''", id="empty span"),
            pytest.param("Killing Eve '   '", id="whitespace-only span"),
        ],
    )
    def test_a_span_that_cannot_be_read_yields_nothing_rather_than_a_guess(self, raw):
        """Every failure mode is closed. `The Americans' sibling The Spy` is the
        one worth naming: its single apostrophe is a possessive with nothing to
        its left to partner, so the answer is no candidate at all rather than
        `The Americans` — which is a real show, and the one the model was
        comparing *against*."""
        assert quoted_candidate(raw) is None

    def test_a_candidate_is_never_the_raw_title_it_came_from(self):
        """The raw already failed to resolve; an identical second query buys
        nothing. Asserted over the corpus because the pairing rule cannot
        currently produce one — the guard is what keeps that true."""
        for raw in (
            "The Americans' sibling 'The Spy'",
            'Killing Eve\'s "Bodyguard"',
            "‘Bodyguard’",
        ):
            assert quoted_candidate(raw) != raw
