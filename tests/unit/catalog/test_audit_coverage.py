"""Every field NEU-1031 classified *modeled* has somewhere to land.

The ticket's central acceptance criterion is "a field in the audit with no column
here is a defect, not a follow-up", which is unverifiable by reading. This
transcribes the audit's inventories — series, season, episode and the taken
namespaces — and asserts each one against the actual table definitions.

It fails in both directions on purpose. A field dropped from the models fails
because its column is gone; a field the audit *skipped* fails if a column for it
quietly appears, because that is a capture decision being reversed without the
document that made it.

**Targets are named by table and column string, never by `Model.__table__`.**
An object reference would resolve through whatever the class is currently called,
so renaming `catalog.show_aka` to something else while keeping the class would
pass silently — and the name is the thing the audit's contract is written in.

Credit tables are NEU-1038's, so the audit's person inventory is listed here as
pending rather than asserted — see `_NEU_1038`.
"""

import pytest

from tvbf.catalog import models as m

# Every entry is either a table name, or a (table, column) pair. Table names are
# unqualified; `_missing` resolves them within the `catalog` schema.
Target = str | tuple[str, str]

# --- series level (audit §3) ------------------------------------------------
# Every measured top-level key of the TMDB series payload, mapped to where it
# lands.
SERIES: dict[str, Target] = {
    "adult": ("show", "adult"),
    "backdrop_path": ("show", "backdrop_path"),
    "created_by": "show_creator",
    "episode_run_time": ("show", "episode_run_time"),
    "first_air_date": ("show", "first_air_date"),
    "genres": "show_genre",
    "homepage": ("show", "homepage"),
    "id": ("show", "tmdb_id"),
    "in_production": ("show", "in_production"),
    "languages": "show_language",
    "last_air_date": ("show", "last_air_date"),
    "last_episode_to_air": ("show", "last_episode_to_air_id"),
    "name": ("show", "name"),
    "next_episode_to_air": ("show", "next_episode_to_air_id"),
    "networks": "show_network",
    "number_of_episodes": ("show", "number_of_episodes"),
    "number_of_seasons": ("show", "number_of_seasons"),
    "origin_country": "show_origin_country",
    "original_language": ("show", "original_language"),
    "original_name": ("show", "original_name"),
    "overview": ("show", "overview"),
    "popularity": ("show", "popularity"),
    "poster_path": ("show", "poster_path"),
    "production_companies": "show_production_company",
    "production_countries": "show_production_country",
    "seasons": "season",
    "spoken_languages": "show_spoken_language",
    "status": ("show", "status"),
    "tagline": ("show", "tagline"),
    "type": ("show", "type"),
    "vote_average": ("show", "vote_average"),
    "vote_count": ("show", "vote_count"),
}

# --- season level (audit §4) ------------------------------------------------
SEASON: dict[str, Target] = {
    "air_date": ("season", "air_date"),
    "episode_count": ("season", "episode_count"),
    "id": ("season", "tmdb_id"),
    "name": ("season", "name"),
    "networks": "season_network",
    "overview": ("season", "overview"),
    "poster_path": ("season", "poster_path"),
    "season_number": ("season", "season_number"),
    "vote_average": ("season", "vote_average"),
    "episodes": "episode",
}

# --- episode level (audit §4) -----------------------------------------------
EPISODE: dict[str, Target] = {
    "air_date": ("episode", "air_date"),
    "episode_number": ("episode", "episode_number"),
    "episode_type": ("episode", "episode_type"),
    "id": ("episode", "tmdb_id"),
    "name": ("episode", "name"),
    "overview": ("episode", "overview"),
    "production_code": ("episode", "production_code"),
    "runtime": ("episode", "runtime"),
    "season_number": ("episode", "season_number"),
    "show_id": ("episode", "show_id"),
    "still_path": ("episode", "still_path"),
    "vote_average": ("episode", "vote_average"),
    "vote_count": ("episode", "vote_count"),
}

# --- the taken namespaces (audit §5) ----------------------------------------
# Ten of the eleven; `aggregate_credits` is NEU-1038's.
NAMESPACES: dict[str, Target] = {
    "alternative_titles": "show_aka",
    "content_ratings": "content_rating",
    "episode_groups": "episode_group",
    "external_ids": ("show", "tvdb_id"),
    "images": "image",
    "keywords": "show_keyword",
    "screened_theatrically": ("episode", "screened_theatrically"),
    "translations": "translation",
    "videos": "video",
    "watch/providers": "show_watch_provider",
}

# The measured `external_ids` set (audit §8), which is richer than the ticket
# assumed. Listed out because `tvdb_id` and `imdb_id` carry the migration's
# mapping tiers and the rest are easy to lose one at a time.
EXTERNAL_IDS = (
    "imdb_id",
    "tvdb_id",
    "tvrage_id",
    "wikidata_id",
    "freebase_id",
    "freebase_mid",
    "facebook_id",
    "instagram_id",
    "twitter_id",
)

# Classified *skipped*, each with a reason the audit records. A column appearing
# for one of these is a decision being reversed silently.
SKIPPED = ("softcore", "recommendations", "similar", "reviews", "credits")

# Modeled by the audit, owned by NEU-1038. Named so the gap is a deliberate
# hand-off rather than an omission this test failed to notice. `created_by` is
# deliberately *not* here: it is a show-creator credit, outside NEU-1038's scope,
# and lands in this ticket as `catalog.show_creator`.
_NEU_1038 = ("aggregate_credits", "crew", "guest_stars")


def _catalog_tables() -> dict[str, object]:
    return {
        table.name: table for table in m.Base.metadata.tables.values() if table.schema == m.SCHEMA
    }


def _missing(target: Target) -> str | None:
    """What is absent, or None if the target resolves."""
    tables = _catalog_tables()
    name, column = target if isinstance(target, tuple) else (target, None)
    if name not in tables:
        return f"no table catalog.{name}"
    if column is not None and column not in tables[name].c:  # type: ignore[attr-defined]
        return f"no column catalog.{name}.{column}"
    return None


@pytest.mark.parametrize(
    ("inventory", "fields"),
    [
        ("series", SERIES),
        ("season", SEASON),
        ("episode", EPISODE),
        ("namespaces", NAMESPACES),
    ],
    ids=["series", "season", "episode", "namespaces"],
)
def test_every_modeled_field_has_a_home(inventory, fields):
    gaps = {field: _missing(target) for field, target in fields.items()}
    gaps = {field: why for field, why in gaps.items() if why}

    assert not gaps, f"{inventory} fields with nowhere to land: {gaps}"


def test_every_measured_external_id_has_a_column():
    missing = [name for name in EXTERNAL_IDS if name not in m.Show.__table__.c]

    assert not missing, f"external_ids fields with no column: {missing}"


@pytest.mark.parametrize("field", SKIPPED)
def test_a_skipped_field_has_no_column(field):
    """Not pedantry. Each of these was skipped on merit — `credits` is strictly
    weaker than `aggregate_credits`, `recommendations` and `similar` are
    TMDB-computed and volatile, `reviews` is somebody else's user-generated
    content. Adding one back is a decision, and should read like one."""
    tables = _catalog_tables()
    surface = {c.name for t in tables.values() for c in t.c} | set(tables)  # type: ignore[attr-defined]

    assert field not in surface


def test_credit_tables_are_still_pending():
    """A canary, not a constraint. When NEU-1038 lands its tables this fails, and
    whoever lands them moves the audit's person inventory into the assertions
    above."""
    assert "person" not in _catalog_tables(), (
        "credit tables have arrived — fold the audit's person inventory "
        f"({', '.join(_NEU_1038)}) into this file's assertions"
    )
