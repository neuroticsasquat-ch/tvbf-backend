"""Parsing tests for the person + guest-cast payloads (pass C, NEU-942).

Guest credits are the one credit shape that carries `_links` rather than
embedded objects, so both ids come out of href strings. A parser that quietly
returns None here yields a person with zero guest credits and no error — the
NEU-922 failure mode — so the fixtures below are verbatim upstream JSON.
"""

from tvbf.tvmaze.api_payloads import TVMazeGuestCastCredit, TVMazePersonDetail

RAW_GUEST = {
    "self": True,
    "voice": False,
    "_links": {
        "episode": {
            "href": "https://api.tvmaze.com/episodes/111196",
            "name": "50 Charades of Grey",
        },
        "character": {
            "href": "https://api.tvmaze.com/characters/115733",
            "name": "Zachary Levi",
        },
    },
}

RAW_PERSON_DETAIL = {
    "id": 30856,
    "url": "https://www.tvmaze.com/people/30856/zachary-levi",
    "name": "Zachary Levi",
    "country": {"name": "United States", "code": "US", "timezone": "America/New_York"},
    "birthday": "1980-09-29",
    "deathday": None,
    "gender": "Male",
    "image": {
        "medium": "https://static.tvmaze.com/m.jpg",
        "original": "https://static.tvmaze.com/o.jpg",
    },
    "updated": 1774528332,
    "_links": {"self": {"href": "https://api.tvmaze.com/people/30856"}},
    "_embedded": {"guestcastcredits": [RAW_GUEST]},
}


def test_guest_credit_parses_ids_out_of_links():
    c = TVMazeGuestCastCredit.model_validate(RAW_GUEST)
    assert c.episode_id == 111196
    assert c.character_id == 115733
    assert c.character_name == "Zachary Levi"
    assert c.is_self is True
    assert c.is_voice is False


def test_guest_credit_with_missing_links_is_skippable():
    c = TVMazeGuestCastCredit.model_validate({"self": False, "voice": False, "_links": {}})
    assert c.episode_id is None and c.character_id is None
    assert c.character_name is None


def test_guest_credit_tolerates_a_null_link_object():
    """`_links` present but an individual link null — must not raise."""
    c = TVMazeGuestCastCredit.model_validate(
        {"self": False, "voice": False, "_links": {"episode": None, "character": None}}
    )
    assert c.episode_id is None and c.character_id is None


def test_guest_credit_ignores_a_non_numeric_href_tail():
    c = TVMazeGuestCastCredit.model_validate(
        {"_links": {"episode": {"href": "https://api.tvmaze.com/episodes/"}}}
    )
    assert c.episode_id is None


def test_person_detail_embeds_guest_cast_credits():
    # `_embedded` is the same alias trap as `self`: a wrong key parses cleanly
    # and yields zero guest credits for all 486k people.
    p = TVMazePersonDetail.model_validate(RAW_PERSON_DETAIL)
    assert p.id == 30856
    assert p.name == "Zachary Levi"
    assert p.country_code == "US"
    assert p.updated == 1774528332
    assert [c.episode_id for c in p.embedded.guestcastcredits] == [111196]


def test_person_detail_without_the_embed_parses_to_an_empty_list():
    p = TVMazePersonDetail.model_validate({"id": 1, "name": "X", "updated": 1})
    assert p.embedded.guestcastcredits == []


def test_person_detail_ignores_cast_and_crew_credits_we_never_request():
    """We deliberately request only guestcastcredits — person-side cast/crew
    credits carry no billing order and would clobber what pass A captured. A
    payload carrying them anyway must parse, and must not surface them."""
    p = TVMazePersonDetail.model_validate(
        {
            "id": 1,
            "name": "X",
            "updated": 1,
            "_embedded": {
                "guestcastcredits": [RAW_GUEST],
                "castcredits": [{"_links": {}}],
                "crewcredits": [{"_links": {}}],
            },
        }
    )
    assert len(p.embedded.guestcastcredits) == 1
    assert not hasattr(p.embedded, "castcredits")
