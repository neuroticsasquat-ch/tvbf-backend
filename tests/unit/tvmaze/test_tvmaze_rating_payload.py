from tvbf.tvmaze.api_payloads import TVMazeEpisode, TVMazeShow


def _show_payload(**overrides):
    base = {
        "id": 1,
        "name": "Foo",
        "updated": 1,
        "network": None,
        "webChannel": None,
        "genres": [],
        "_embedded": {"episodes": [], "seasons": []},
    }
    base.update(overrides)
    return base


def test_show_with_rating_average():
    show = TVMazeShow.model_validate(_show_payload(rating={"average": 8.5}))
    assert show.rating_average == 8.5


def test_show_with_none_rating_average():
    show = TVMazeShow.model_validate(_show_payload(rating={"average": None}))
    assert show.rating_average is None


def test_show_missing_rating_block():
    show = TVMazeShow.model_validate(_show_payload())
    assert show.rating is None
    assert show.rating_average is None


def test_episode_with_rating_average():
    ep = TVMazeEpisode.model_validate(
        {
            "id": 10,
            "season": 1,
            "number": 1,
            "rating": {"average": 7.2},
        }
    )
    assert ep.rating_average == 7.2
