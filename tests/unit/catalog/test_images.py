"""The composition side of NEU-1063.

These are the tests that make the mapping in `catalog/images.py` a contract
rather than a comment: what each API field name resolves to, and what an absent
path resolves to.
"""

import pytest

from tvbf.catalog import models as m
from tvbf.catalog.images import (
    BACKDROP,
    KINDS,
    POSTER,
    PROFILE,
    STILL,
    ImageKind,
    image_pair,
    image_url,
    medium_url,
    poster_urls,
    profile_urls,
    still_urls,
)
from tvbf.config import get_settings


@pytest.fixture
def base_url(monkeypatch):
    """Pin the image base URL, and leave no cached `Settings` behind.

    `get_settings` is `lru_cache`d, so a test that sets the env without clearing
    it either reads a stale value or hands one to whatever runs next.
    """
    monkeypatch.setenv("TMDB_IMAGE_BASE_URL", "https://images.example/t/p")
    get_settings.cache_clear()
    yield "https://images.example/t/p"
    get_settings.cache_clear()


# --- the mapping itself -----------------------------------------------------


def test_the_exposed_sizes_are_the_ones_this_ticket_chose(base_url):
    """The size mapping, asserted by name. Changing one is a visible diff here
    and not only in a docstring."""
    assert (POSTER.medium, POSTER.original) == ("w342", "original")
    assert (STILL.medium, STILL.original) == ("w300", "original")
    assert (PROFILE.medium, PROFILE.original) == ("w185", "original")
    assert (BACKDROP.medium, BACKDROP.original) == ("w780", "original")


def test_every_exposed_size_is_one_tmdb_actually_offers():
    """`available` is the 2026-08-12 `/configuration` reading. A size outside it
    is answered by TMDB with a placeholder image rather than an error, so this
    is the only place the mistake is visible."""
    for kind in KINDS:
        assert kind.medium in kind.available
        assert kind.original in kind.available


def test_a_size_upstream_does_not_offer_is_rejected_at_call_time_too():
    """TMDB answers an unknown size with a placeholder image and a 200, so this
    is the only layer that can notice."""
    with pytest.raises(ValueError, match="'w999' is not one of TMDB's poster sizes"):
        image_url("/abc.jpg", POSTER, "w999")

    # A still size is wrong for a poster even though it is a real TMDB size.
    with pytest.raises(ValueError, match="not one of TMDB's poster sizes"):
        image_url("/abc.jpg", POSTER, "w300")


def test_a_size_upstream_does_not_offer_is_rejected_at_construction():
    with pytest.raises(ValueError, match="poster medium size 'w999'"):
        ImageKind(name="poster", available=("w342", "original"), medium="w999")

    with pytest.raises(ValueError, match="poster original size 'huge'"):
        ImageKind(name="poster", available=("w342", "original"), medium="w342", original="huge")


# --- composition ------------------------------------------------------------


def test_a_path_composes_into_a_working_url(base_url):
    assert image_url("/abc.jpg", POSTER, POSTER.medium) == f"{base_url}/w342/abc.jpg"
    assert image_url("/abc.jpg", POSTER, POSTER.original) == f"{base_url}/original/abc.jpg"


def test_the_base_url_is_read_from_config_not_hardcoded(monkeypatch):
    monkeypatch.setenv("TMDB_IMAGE_BASE_URL", "https://cdn.example/img")
    get_settings.cache_clear()
    try:
        assert image_url("/abc.jpg", STILL, STILL.medium) == "https://cdn.example/img/w300/abc.jpg"
    finally:
        get_settings.cache_clear()


def test_a_trailing_slash_on_the_base_url_does_not_double_up(monkeypatch):
    """TMDB's own `/configuration` reports `secure_base_url` **with** a trailing
    slash while our default carries none, so both spellings reach this code."""
    monkeypatch.setenv("TMDB_IMAGE_BASE_URL", "https://image.tmdb.org/t/p/")
    get_settings.cache_clear()
    try:
        assert image_url("/abc.jpg", POSTER, "w342") == "https://image.tmdb.org/t/p/w342/abc.jpg"
    finally:
        get_settings.cache_clear()


def test_a_path_without_a_leading_slash_still_composes(base_url):
    """Every path TMDB sends starts with `/`; a stored value that lost it would
    otherwise silently produce `.../w342abc.jpg`."""
    assert image_url("abc.jpg", POSTER, "w342") == f"{base_url}/w342/abc.jpg"


# --- absence ----------------------------------------------------------------


@pytest.mark.parametrize("absent", [None, "", "   "])
def test_an_absent_path_is_null_rather_than_a_url_that_404s(base_url, absent):
    """A null image is normal, not exceptional: NEU-1042 copied no TV Maze image
    URLs into `poster_path`, so every un-ingested show has none."""
    assert image_url(absent, POSTER, "w342") is None
    for kind in KINDS:
        assert image_pair(absent, kind) == (None, None)


# --- the pairs the response models consume ----------------------------------


def test_each_helper_returns_the_medium_original_pair_for_its_kind(base_url):
    assert poster_urls("/p.jpg") == (f"{base_url}/w342/p.jpg", f"{base_url}/original/p.jpg")
    assert still_urls("/s.jpg") == (f"{base_url}/w300/s.jpg", f"{base_url}/original/s.jpg")
    assert profile_urls("/h.jpg") == (f"{base_url}/w185/h.jpg", f"{base_url}/original/h.jpg")


def test_every_kind_composes_both_halves_of_its_pair(base_url):
    """Unpacked straight into a response model, a half-null pair would render a
    thumbnail whose full-size link 404s."""
    for kind in KINDS:
        medium, original = image_pair("/x.jpg", kind)
        assert medium == f"{base_url}/{kind.medium}/x.jpg"
        assert original == f"{base_url}/original/x.jpg"


def test_the_single_field_helper_returns_the_medium_of_its_kind(base_url):
    """`PersonRef`, `ShowRef` and `CharacterRef` expose `image_medium` alone."""
    assert medium_url("/h.jpg", PROFILE) == f"{base_url}/w185/h.jpg"
    assert medium_url("/p.jpg", POSTER) == f"{base_url}/w342/p.jpg"
    assert medium_url(None, PROFILE) is None


# --- the columns these are composed from ------------------------------------


def test_each_catalog_column_composes_through_the_kind_that_matches_it(base_url):
    """The association NEU-1047 must not get wrong, pinned here rather than left
    to the caller's memory.

    An episode still composed as a poster is a `w342` URL that TMDB answers with
    a placeholder rather than an error, so nothing below the rendered page would
    notice. This is as close as this ticket can get to "every image field
    resolves to a working URL for a TMDB-sourced show" — the routes still read
    `tvmaze`, and NEU-1047 is the pass that repoints them.
    """
    show = m.Show(tmdb_id=1, name="A Show", poster_path="/show.jpg", backdrop_path="/back.jpg")
    season = m.Season(tmdb_id=2, show_id=1, season_number=1, poster_path="/season.jpg")
    episode = m.Episode(
        tmdb_id=3, show_id=1, season_number=1, episode_number=1, still_path="/still.jpg"
    )
    person = m.Person(tmdb_id=4, name="A Person", profile_path="/person.jpg")

    assert poster_urls(show.poster_path) == (
        f"{base_url}/w342/show.jpg",
        f"{base_url}/original/show.jpg",
    )
    assert poster_urls(season.poster_path) == (
        f"{base_url}/w342/season.jpg",
        f"{base_url}/original/season.jpg",
    )
    assert still_urls(episode.still_path) == (
        f"{base_url}/w300/still.jpg",
        f"{base_url}/original/still.jpg",
    )
    assert profile_urls(person.profile_path) == (
        f"{base_url}/w185/person.jpg",
        f"{base_url}/original/person.jpg",
    )
    # No response field exposes a backdrop today — the mapping is recorded, not
    # wired. See ADR-0010.
    assert image_pair(show.backdrop_path, BACKDROP) == (
        f"{base_url}/w780/back.jpg",
        f"{base_url}/original/back.jpg",
    )


def test_a_show_tmdb_never_matched_composes_to_null_rather_than_a_broken_url(base_url):
    """NEU-1042 copied no TV Maze image URLs into `poster_path`, so an
    un-ingested or unmatched show carries none at all."""
    copied = m.Show(tmdb_id=None, name="Locally authored", poster_path=None)
    assert poster_urls(copied.poster_path) == (None, None)
