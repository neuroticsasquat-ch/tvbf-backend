"""Parsing tests for the person payload.

`/people/{id}` carries no credit embed since ADR-0003 moved every credit table
to the show axis, so what is left to get wrong is the attribute set — and
`updated` in particular, which is the person delta's ordering key.
"""

import pytest
from pydantic import ValidationError

from tvbf.tvmaze.api_payloads import TVMazePerson

RAW_PERSON = {
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
}


def test_person_parses_the_full_attribute_set():
    p = TVMazePerson.model_validate(RAW_PERSON)
    assert p.id == 30856
    assert p.name == "Zachary Levi"
    assert p.country_code == "US"
    assert p.country_name == "United States"
    assert p.timezone == "America/New_York"
    assert p.gender == "Male"
    assert p.image is not None and p.image.medium == "https://static.tvmaze.com/m.jpg"
    assert p.updated == 1774528332


def test_person_coerces_empty_date_strings_to_none():
    p = TVMazePerson.model_validate({**RAW_PERSON, "birthday": "", "deathday": ""})
    assert p.birthday is None and p.deathday is None


def test_person_ignores_credit_embeds_we_no_longer_request():
    """A payload that carries them anyway must parse, and must not surface
    them — the show and season fetches own every credit table now."""
    p = TVMazePerson.model_validate(
        {**RAW_PERSON, "_embedded": {"guestcastcredits": [{"_links": {}}]}}
    )
    assert p.id == 30856
    assert not hasattr(p, "embedded")


def test_person_without_updated_is_a_parse_error():
    """Defaulting it would let a payload missing `updated` silently reset a
    person's watermark on re-upsert."""
    with pytest.raises(ValidationError):
        TVMazePerson.model_validate({"id": 1, "name": "X"})
