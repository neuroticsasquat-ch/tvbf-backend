"""The airdate correction (NEU-1145).

TMDB carries one contributor-entered calendar date per episode and no regional
model at all, so a season entered against the US west coast records the Pacific
day and reads a day early for everyone else. `client.py` is the TV Maze oracle
that settles which of the two a season used; `reconcile.py` is the nightly pass
that turns its answer into `catalog.air_date_offset` rows. Applying an offset is
`catalog/offsets.py`'s job, not this package's.
"""
