"""Pydantic shapes for parsing the TV Maze oracle's JSON (NEU-1145).

The `tmdb/api_payloads.py` role for the one upstream this repo reads without
mirroring. The carve-out is the same: shapes that parse somebody else's JSON stay
out of `schemas.py`, which is our own API's contract.

**The shape is as narrow as the extraction.** TV Maze sends a full episode
object; three fields are parsed and the rest is dropped at the boundary, which
is what makes "we store one integer per `(show, season)` and never TV Maze's
titles, numbering or dates" a property of the parser rather than a promise made
downstream — and that minimisation is what the CC BY-SA position in NEU-1145 §6
rests on.

**`season` and `number` are optional, and `number` is the reason.** TV Maze
numbers a special `null`, which is exactly the row that cannot be paired with
anything on our side. A required field would fail the whole show's parse on a
value the comparison is going to discard anyway.
"""

from datetime import date
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _empty_to_none(v: Any) -> Any:
    """`""` means "unknown" upstream, in the same places `null` does."""
    if v == "":
        return None
    return v


# Restated rather than imported from `tmdb/api_payloads.py`, on the precedent
# `tmdb/client.py:is_gone_upstream` sets: this is one alias over one validator
# with no TMDB content in it, and an import would tie the oracle's parser to the
# lifetime of the mirror's. TV Maze returns `""` for an unscheduled episode's
# `airdate`, so the coercion is load-bearing here and not merely inherited.
OptionalDate = Annotated[date | None, BeforeValidator(_empty_to_none)]


class TVMazeShowRef(BaseModel):
    """A `/lookup/shows` hit. Only the id is read — it is all the next call needs."""

    model_config = ConfigDict(extra="ignore")

    tvmaze_id: int = Field(alias="id")


class TVMazeEpisode(BaseModel):
    """One entry of `/shows/{id}/episodes`.

    `airdate` is the whole point of the request and the only value that survives
    it, as one term of a subtraction.
    """

    model_config = ConfigDict(extra="ignore")

    season: int | None = None
    number: int | None = None
    airdate: OptionalDate = None
