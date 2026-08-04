"""Parsing tests for `/seasons/{id}/episodes?embed[]=guestcast&embed[]=guestcrew`.

Fixtures are verbatim upstream shapes. Two aliases carry the whole payload:
`_embedded`, and the camelCase `guestCrewType` that episode crew uses where
show-level crew uses `type`. Get either wrong and episodes parse cleanly with
zero credits — indistinguishable from an episode that genuinely has none, which
is exactly the failure mode that would survive a 29-hour pass unnoticed.
"""

from tvbf.tvmaze.api_payloads import TVMazeSeasonEpisode

RAW_PERSON = {
    "id": 30856,
    "name": "Zachary Levi",
    "country": {"name": "United States", "code": "US", "timezone": "America/New_York"},
    "birthday": "1980-09-29",
    "deathday": None,
    "gender": "Male",
    "image": {"medium": "https://static.tvmaze.com/m.jpg", "original": None},
    "updated": 1774528332,
}

RAW_EPISODE = {
    "id": 111196,
    "season": 2,
    "number": 3,
    "name": "50 Charades of Grey",
    "airdate": "2017-02-14",
    "airtime": "20:00",
    "runtime": 60,
    "_embedded": {
        "guestcast": [
            {
                "person": RAW_PERSON,
                "character": {"id": 115733, "name": "Himself", "image": None},
                "self": True,
                "voice": False,
            }
        ],
        "guestcrew": [
            {"person": RAW_PERSON, "guestCrewType": "Director"},
            {"person": RAW_PERSON, "guestCrewType": "Teleplay"},
        ],
    },
}


def test_episode_parses_both_credit_embeds():
    ep = TVMazeSeasonEpisode.model_validate(RAW_EPISODE)

    assert ep.id == 111196
    assert ep.season == 2 and ep.number == 3

    (guest,) = ep.embedded.guestcast
    assert guest.person.id == 30856
    assert guest.character.id == 115733
    assert guest.is_self is True and guest.is_voice is False

    assert [c.type for c in ep.embedded.guestcrew] == ["Director", "Teleplay"]
    assert all(c.person.id == 30856 for c in ep.embedded.guestcrew)


def test_crew_type_is_read_from_the_camel_case_key():
    """Show-level crew sends `type`; episode-level crew sends `guestCrewType`.
    Reading the wrong one raises on every episode that has crew."""
    ep = TVMazeSeasonEpisode.model_validate(
        {
            "id": 1,
            "season": 1,
            "number": 1,
            "_embedded": {"guestcrew": [{"person": RAW_PERSON, "guestCrewType": "Writer"}]},
        }
    )
    assert [c.type for c in ep.embedded.guestcrew] == ["Writer"]


def test_an_episode_with_no_embed_parses_to_empty_lists():
    ep = TVMazeSeasonEpisode.model_validate({"id": 2, "season": 1, "number": 1})
    assert ep.embedded.guestcast == []
    assert ep.embedded.guestcrew == []


def test_a_special_parses_with_a_null_number():
    """This route includes specials without a `specials=1` param, which is half
    the reason it is the route we use."""
    ep = TVMazeSeasonEpisode.model_validate(
        {"id": 3, "season": 1, "number": None, "name": "Behind the Scenes", "airdate": ""}
    )
    assert ep.number is None
    assert ep.airdate is None  # "" coerced, not a parse error


def test_credit_order_is_preserved():
    """`sort_order` is the array index, so the array order is load-bearing."""
    ep = TVMazeSeasonEpisode.model_validate(
        {
            "id": 4,
            "season": 1,
            "number": 1,
            "_embedded": {
                "guestcast": [
                    {
                        "person": {**RAW_PERSON, "id": pid},
                        "character": {"id": 900 + pid, "name": f"C{pid}"},
                    }
                    for pid in (3, 1, 2)
                ]
            },
        }
    )
    assert [c.person.id for c in ep.embedded.guestcast] == [3, 1, 2]
