"""The one place `in_my_shows` is declared (NEU-1184 §2.1).

Two decisions are asserted here rather than left to the four surfaces that
inherit them, because both fail *silently* if undone:

* **The field is required, with no default.** A `= False` default — which is
  what trending and anticipated each carried before they were re-parented —
  lets a fifth surface forget to pass it and serve `false` for a show the
  viewer tracks. Required makes that a type error instead.
* **It is not on `ShowSummary`.** That type is nested in six `/me` payloads and
  is `ShowDetail`'s base, so a field on the base would emit `in_my_shows: false`
  on every My Shows row, where the truth is always `true`.
"""

import pytest
from pydantic import ValidationError

from tvbf.catalog.schemas import (
    AnticipatedShowOut,
    BrowseShowOut,
    MarkedShowOut,
    ShowDetail,
    ShowSummary,
    SimilarShowOut,
    TrendingShowOut,
)


@pytest.mark.parametrize(
    "cls",
    [MarkedShowOut, BrowseShowOut, SimilarShowOut, TrendingShowOut, AnticipatedShowOut],
)
def test_the_mark_is_required_on_every_marked_shape(cls):
    with pytest.raises(ValidationError):
        cls(id=1, name="A Show")
    assert cls(id=1, name="A Show", in_my_shows=True).in_my_shows is True


@pytest.mark.parametrize("cls", [ShowSummary, ShowDetail])
def test_the_unmarked_shapes_do_not_carry_it(cls):
    assert "in_my_shows" not in cls.model_fields


@pytest.mark.parametrize(
    "cls", [BrowseShowOut, SimilarShowOut, TrendingShowOut, AnticipatedShowOut]
)
def test_the_four_surfaces_inherit_it_rather_than_declare_it(cls):
    """Four subclasses, one declaration — what the surfaces disagree about is
    everything around the field, which is why each keeps its own type."""
    assert issubclass(cls, MarkedShowOut)
    assert "in_my_shows" not in cls.__annotations__


@pytest.mark.parametrize("cls", [TrendingShowOut, AnticipatedShowOut])
def test_re_parenting_left_the_served_shape_alone(cls):
    """`/trending` and `/anticipated` serve byte-identical bodies (NEU-1184 §9
    AC 2), which is a claim about field *order* as well as membership: both
    declared the mark last, on their own class, and inheriting it from
    `MarkedShowOut` has to leave it in the same place. Moving the declaration
    ahead of `ShowSummary`'s fields would reorder every key in both payloads
    without failing a single route test.
    """
    assert list(cls.model_fields) == [*ShowSummary.model_fields, "in_my_shows"]
