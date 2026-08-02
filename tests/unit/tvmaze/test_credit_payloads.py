"""Parsing tests for the cast/crew embed payloads.

Every fixture here is verbatim upstream JSON rather than a hand-simplified
dict. This is the NEU-922 lesson: a field whose alias never matched produced
no error at all, it just silently wrote NULL for months.
"""

from tvbf.tvmaze.api_payloads import (
    TVMazeCastEntry,
    TVMazeCrewEntry,
    TVMazePerson,
    TVMazeShow,
)

RAW_CAST = {
    "person": {
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
    },
    "character": {
        "id": 45090,
        "url": "https://www.tvmaze.com/characters/45090/chuck",
        "name": 'Charles "Chuck" Bartowski',
        "image": {"medium": "https://static.tvmaze.com/cm.jpg", "original": None},
    },
    "self": False,
    "voice": False,
}

RAW_CREW = {
    "type": "Co-Executive Producer",
    "person": {
        "id": 795,
        "name": "Matthew Miller",
        "country": None,
        "birthday": None,
        "deathday": None,
        "gender": "Male",
        "image": None,
        "updated": 1738338751,
    },
}


def test_cast_entry_parses_person_character_and_flags():
    e = TVMazeCastEntry.model_validate(RAW_CAST)
    assert e.person.id == 30856
    assert e.person.name == "Zachary Levi"
    assert e.person.country_code == "US"
    assert e.person.country_name == "United States"
    assert e.person.timezone == "America/New_York"
    assert e.person.birthday is not None and e.person.birthday.isoformat() == "1980-09-29"
    assert e.person.deathday is None
    assert e.person.gender == "Male"
    assert e.person.image is not None
    assert e.person.image.medium == "https://static.tvmaze.com/m.jpg"
    assert e.person.image.original == "https://static.tvmaze.com/o.jpg"
    assert e.person.updated == 1774528332
    assert e.character.id == 45090
    assert e.character.name == 'Charles "Chuck" Bartowski'
    assert e.character.image is not None
    assert e.character.image.medium == "https://static.tvmaze.com/cm.jpg"
    assert e.character.image.original is None
    assert e.is_self is False and e.is_voice is False


def test_cast_self_flag_parses_from_reserved_name():
    # `self` is aliased; a naive field name silently never populates.
    e = TVMazeCastEntry.model_validate({**RAW_CAST, "self": True, "voice": True})
    assert e.is_self is True and e.is_voice is True


def test_crew_entry_parses_type_and_person():
    e = TVMazeCrewEntry.model_validate(RAW_CREW)
    assert e.type == "Co-Executive Producer"
    assert e.person.id == 795
    assert e.person.name == "Matthew Miller"
    assert e.person.country_code is None
    assert e.person.country_name is None
    assert e.person.timezone is None
    assert e.person.image is None
    assert e.person.updated == 1738338751


def test_person_empty_string_dates_become_none():
    # TV Maze sends "" not null for unknown dates. A bare `date | None` raises.
    p = TVMazePerson.model_validate(
        {"id": 1, "name": "X", "birthday": "", "deathday": "", "updated": 1}
    )
    assert p.birthday is None and p.deathday is None


def test_show_payload_embeds_cast_and_crew():
    # The `_embedded` alias is the same trap as `self`: a wrong key here parses
    # cleanly and yields empty credit lists for every show.
    show = TVMazeShow.model_validate(
        {
            "id": 168,
            "name": "Chuck",
            "updated": 1700000000,
            "genres": [],
            "_embedded": {
                "seasons": [{"id": 1, "number": 1}],
                "cast": [RAW_CAST],
                "crew": [RAW_CREW],
            },
        }
    )
    assert [s.id for s in show.embedded.seasons] == [1]
    assert [c.person.id for c in show.embedded.cast] == [30856]
    assert [c.person.id for c in show.embedded.crew] == [795]
    # Not requesting the episodes embed must not error — it just stays empty.
    assert show.embedded.episodes == []
