"""The order episodes are read in, and why a negative number sorts last.

`catalog.episode.episode_number` is NOT NULL — under TMDB a special is season 0
with a real number (audit D2) — where `tvmaze.episode.number` was NULL for one.
NEU-1042's copy had to put *something* in the column for the 27,498 TV Maze
specials it carried across, and chose **negative numbers within the real
season**, so the value reads as invented rather than as an episode that could be
confused with a real one.

A plain `ORDER BY season_number, episode_number` would therefore move every one
of those specials from the end of its season to the **front** of it, because
Postgres sorted the old NULLs last on ASC and sorts -1 first. That is a visible
change to a season's episode list, on exactly the rows nobody chose the number
for, so the ordering carries an explicit "synthetic last" term instead.

A TMDB-native special is untouched by this: it lives in season 0 and sorts ahead
of season 1, which is upstream's own model rather than an artefact of ours.

The same invention must not reach the API either — `EpisodeOut.number` was
**null** for every one of those rows before the repoint, and NEU-1047's
acceptance criterion admits exactly one changed payload (a show's season list).
`public_number` maps a negative back to `None`, so a client sees what it always
saw and nobody has to learn that -1 means "special".

## What a special is (NEU-1062)

This module is the **only** place that decides. A special is two things, and
neither will ever become the other:

- **Season 0 with a real episode number** — TMDB's own model (audit D2), 106,584
  episodes across 12,151 shows.
- **A negative `episode_number` inside a real season** — the copy's invention
  above, 20,973 episodes across 5,051 shows, 156 of them watched. NEU-1126 kept
  exactly the ones with no TMDB counterpart, so they are locally-authored rows
  (ADR-0008) no ingest or delta will ever revisit. Re-homing them into a minted
  season 0 was rejected: it would move rows `app.user_episode_watch` points at to
  buy tidiness in a representation deliberately made to look invented.

Verified against production 2026-08-12: no `season_number` anywhere in `catalog`
is negative, so `season_number == 0` is a total, unambiguous test for the first.

`IS_SPECIAL` is both; `IS_COPIED_SPECIAL` is only the second. The distinction is
load-bearing rather than cosmetic — show-level progress strips both, because
neither should count toward "how far through am I", while **per-season** progress
strips only the copied ones, so season 0's own row can report its own contents
(`3/9 specials watched` is useful and true). There is deliberately no
`regular_episodes()` base selectable and no paired `count_regular_*` variant:
each call site names its own predicate, because no default is right often enough
and a forgotten override would fail silently in the dangerous direction. The
ledger test in `tests/integration/app/repos/test_specials_ledger.py` enumerates
every site and its treatment, which is what catches the next one.
"""

from sqlalchemy import or_

from tvbf.catalog import models as m

SPECIALS_SEASON_NUMBER = 0
"""The season number TMDB gives a show's specials, and we sort last."""

IS_COPIED_SPECIAL = m.Episode.episode_number < 0
"""A TV Maze special NEU-1042 copied in with an invented negative number."""

IS_SPECIAL = or_(m.Episode.season_number == SPECIALS_SEASON_NUMBER, IS_COPIED_SPECIAL)
"""Either kind of special: TMDB's season 0, or a copied negative number."""


def is_specials_season(season_number: int) -> bool:
    """The Python-side half of `SPECIALS_SEASON_NUMBER`, for row objects."""
    return season_number == SPECIALS_SEASON_NUMBER


# `episode_number < 0` is false (0) for a real episode and true (1) for a copied
# special, so ascending on it puts the invented numbers after the real ones
# within their season — where the NULLs they replaced used to land.
_SYNTHETIC_LAST = (m.Episode.episode_number < 0).asc()

# The same trick one grain up: season 0 sorts *ahead* of season 1 under TMDB's
# own model, so 12,151 shows list their specials first. Ascending on the boolean
# moves the whole season to the end of the show without disturbing anything
# inside it (NEU-1062).
_SPECIALS_SEASON_LAST = (m.Episode.season_number == SPECIALS_SEASON_NUMBER).asc()

EPISODE_ORDER = (
    _SPECIALS_SEASON_LAST,
    m.Episode.season_number.asc(),
    _SYNTHETIC_LAST,
    m.Episode.episode_number.asc(),
)
"""Regular seasons in order, then Specials — copied specials last within each."""


def public_number(episode: m.Episode) -> int | None:
    """The episode number the API exposes: `None` for a copied special.

    Every builder that serialises an episode number goes through here. A bare
    `episode.episode_number` would publish the -1 the copy invented, which is a
    payload change on 27,498 rows — 156 of them watched — outside the one
    exception this ticket is allowed.
    """
    return episode.episode_number if episode.episode_number >= 0 else None
