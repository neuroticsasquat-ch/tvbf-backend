from tvbf.tvmaze.schemas import TVMazeAka


def test_parses_full_aka_payload():
    aka = TVMazeAka.model_validate(
        {
            "name": "Tokyo Revengers",
            "country": {"code": "US", "name": "United States", "timezone": "America/New_York"},
            "language": "en",
        }
    )
    assert aka.name == "Tokyo Revengers"
    assert aka.country_code == "US"
    assert aka.country_name == "United States"
    assert aka.language == "en"


def test_parses_aka_with_no_country_or_language():
    aka = TVMazeAka.model_validate({"name": "Альфа", "country": None})
    assert aka.name == "Альфа"
    assert aka.country_code is None
    assert aka.country_name is None
    assert aka.language is None


def test_ignores_extra_fields():
    aka = TVMazeAka.model_validate(
        {"name": "Foo", "country": None, "language": None, "_links": {"self": "http://x"}}
    )
    assert aka.name == "Foo"
