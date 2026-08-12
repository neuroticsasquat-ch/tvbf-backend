"""Which season rows a read path shows, when a show carries two for one number.

**This is the one payload NEU-1047 allows to differ, and this module is the
decision.** Everything else about the API is byte-identical across the repoint;
a show's season list is not, because `catalog.season` genuinely holds more rows
than TMDB named.

## Why the duplicates exist

NEU-1042 copied every TV Maze season in with `tmdb_id IS NULL`, and nothing ever
mapped that grain — by the time a mapping pass could have run, the ingest had
taken every `tmdb_id` and `uq_season_tmdb_id` refused it. NEU-1119 deleted the
122,350 copies that had an ingested counterpart to defer to, and **deliberately
kept 18,339 that did not**, across 4,396 shows: TMDB lacking a season number is
not permission to delete TV Maze data no feed can restore. Two shapes survive
(measured against production 2026-08-11):

1. **Two numbering schemes side by side** — 10,193 seasons across 929 shows.
   TMDB numbers `1..N`; TV Maze numbers long-running programmes by calendar year.
   *Talking Movies* carries ingested seasons 1–27 **and** copied seasons
   2006–2026, so 52 rows describe about 27 seasons.
2. **Seasons TMDB has no counterpart for** — 8,146, mostly announced-but-unaired
   on the shows users actually track: *True Detective* 5, *Shōgun* 2–3, *Only
   Murders in the Building* 6, each with zero episodes.

A third, smaller residue: 33 `(show, season number)` pairs are still doubled at
the *same* number across 9 shows — TV Maze's own duplicate numbering, both rows
copied, no user tracking any of them.

## The rule

**One row per `(show_id, season_number)`, preferring the row that carries a
`tmdb_id`.** Ties — two copies at one number — break on the lowest id, which is
the older row.

That is it. `deduped` is a pure function over rows a caller has already loaded,
because every read path that needs it (`GET /shows/{id}`, `GET
/shows/{id}/seasons`, `/me/upcoming/seasons`) already selects a show's seasons in
full, and a SQL-side `DISTINCT ON` would put the rule in three places instead of
one.

## What it deliberately does not do

**It does not collapse the year-vs-ordinal split**, and that is the hard part of
the decision rather than an omission. *Talking Movies* keeps 52 entries. The two
numbering schemes never collide, so no rule keyed on the season number can tell
them apart — and the only rule that could is "hide the copied set", which is
exactly what the ticket's hard constraint forbids: the copied seasons hold
episodes that exist nowhere else. *Will & Grace* is the case that settles it.
TMDB models the revival as a separate series, so seasons 9–11 exist **only** as
copied rows, two of them carrying real watch history (17 records across seasons 9
and 10). A read path that preferred "the ingested set, when a show has one" would
hide episodes a user has already marked watched, which is the one thing this
migration promised not to do.

So the cost is paid where it is cheapest: the year-vs-ordinal shows render a long
season picker, and none of the worst-affected 929 is tracked by anyone. Hiding
the copies would be lossy for a real user; showing them is untidy for nobody.

`tmdb_id IS NULL` remains the marker (ADR-0008) if a later frontend ticket wants
to render a locally-authored season distinctly — the rows reaching the SPA are
unchanged in shape, so that stays available without another backend pass.
"""

from collections.abc import Iterable

from tvbf.catalog import models as m


def _preference(season: m.Season) -> tuple[int, int]:
    """Sort key deciding which of two rows at one season number survives.

    Ingested first (`tmdb_id IS NOT NULL`), then the lowest id. Both halves are
    total orders over the rows, so the choice is deterministic — which matters
    because `catalog.season` deliberately carries no
    `UNIQUE (show_id, season_number)` and Postgres is free to return the pair in
    either order.
    """
    return (0 if season.tmdb_id is not None else 1, season.id)


def deduped(seasons: Iterable[m.Season]) -> list[m.Season]:
    """One season per `(show_id, season_number)`, ordered by show then number.

    Safe to call with rows spanning several shows: the key includes `show_id`,
    so `/me/upcoming/seasons` can hand it a whole My Shows list at once.
    """
    best: dict[tuple[int, int], m.Season] = {}
    for season in seasons:
        key = (season.show_id, season.season_number)
        incumbent = best.get(key)
        if incumbent is None or _preference(season) < _preference(incumbent):
            best[key] = season
    return [best[key] for key in sorted(best)]
