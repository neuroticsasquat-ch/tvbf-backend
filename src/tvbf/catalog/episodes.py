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
"""

from tvbf.catalog import models as m

# `episode_number < 0` is false (0) for a real episode and true (1) for a copied
# special, so ascending on it puts the invented numbers after the real ones
# within their season — where the NULLs they replaced used to land.
_SYNTHETIC_LAST = (m.Episode.episode_number < 0).asc()

EPISODE_ORDER = (m.Episode.season_number.asc(), _SYNTHETIC_LAST, m.Episode.episode_number.asc())
"""Season, then real episodes in number order, then any copied specials."""


def public_number(episode: m.Episode) -> int | None:
    """The episode number the API exposes: `None` for a copied special.

    Every builder that serialises an episode number goes through here. A bare
    `episode.episode_number` would publish the -1 the copy invented, which is a
    payload change on 27,498 rows — 156 of them watched — outside the one
    exception this ticket is allowed.
    """
    return episode.episode_number if episode.episode_number >= 0 else None
