"""The handle rule (NEU-1163 §1-§3), asserted against both write sites at once.

`Handle` is one `Annotated` alias shared by `SignupRequest` and
`HandleUpdateRequest`, so every case below is parametrised over both classes:
the whole point of the alias is that one input gets one verdict at both doors.
A test naming only one of them would pass while the two disagreed.

Everything here is a **schema** rule, which is what makes it a 422 carrying
`loc: ["body", "handle"]`. Uniqueness is not — it needs a session, and it
answers 409 from the service layer instead.
"""

import re

import pytest
from pydantic import ValidationError

from tvbf.app.handles import RESERVED_HANDLES
from tvbf.app.schemas import HandleUpdateRequest, SignupRequest

_SIGNUP_REST = {
    "email": "someone@example.com",
    "password": "correct horse battery",
    "display_name": "Someone",
    "invite_code": "invite-code",
}


def _validate(model: type, value: str) -> str:
    if model is SignupRequest:
        return SignupRequest(handle=value, **_SIGNUP_REST).handle
    return HandleUpdateRequest(handle=value).handle


BOTH = pytest.mark.parametrize("model", [SignupRequest, HandleUpdateRequest])


@BOTH
@pytest.mark.parametrize(
    "raw",
    ["TomBoone", "@TomBoone", "  @tomboone ", "tomboone", "@tomboone"],
)
def test_case_and_sigil_are_normalised_not_refused(model, raw):
    """§1.1. All five spellings are one account, and none of them is an error.

    A user who types their own name the way they capitalise it, or pastes a
    handle with the sigil they saw it printed with, gets the account they meant.
    """
    assert _validate(model, raw) == "tomboone"


@BOTH
@pytest.mark.parametrize(
    "value",
    [
        "ab",  # under the 3-character floor
        "a_very_long_handle_of_thirty_one",  # over the 30-character ceiling
        "9lives",  # does not start with a letter
        "_tom",  # does not start with a letter
        "tom-boone",  # hyphen is outside the charset
        "tom.boone",  # so is a dot
        "tom boone",  # and a space
        "admin",  # reserved
        "support",  # reserved
        "settings",  # reserved: an SPA route
        "my_shows",  # reserved: an SPA route, in its claimable spelling
        "",
    ],
)
def test_refusals(model, value):
    with pytest.raises(ValidationError):
        _validate(model, value)


@BOTH
def test_exactly_one_sigil_is_stripped(model):
    """`@@tom_b` is not a handle anybody was handed; accepting it would make the
    normalisation a second, looser rule rather than a spelling correction."""
    assert _validate(model, "@tom_b") == "tom_b"
    with pytest.raises(ValidationError):
        _validate(model, "@@tom_b")


@BOTH
def test_the_anonymisation_shape_is_refused_by_pattern_not_by_prefix(model):
    """§1.2. `user_<8 hex>` is what the backfill and `refresh_db.sh` produce, so
    leaving it claimable would let a stranger wear an identifier a real account
    either holds or recently held. The refusal is the *shape*: `user_notahex` is
    an ordinary handle and is accepted."""
    with pytest.raises(ValidationError):
        _validate(model, "user_3f4a2b1c")
    assert _validate(model, "user_notahex") == "user_notahex"


@BOTH
@pytest.mark.parametrize("value", ["tom_boone", "tom99", "abc", "a" * 30])
def test_accepts(model, value):
    assert _validate(model, value) == value


def test_every_reserved_entry_is_itself_a_claimable_shape():
    """§3.2 / AC. An entry outside the charset could never be claimed anyway, so
    carrying it would be noise — and one that is silently unreachable is how a
    list stops meaning what it says."""
    shape = re.compile(r"^[a-z][a-z0-9_]{2,29}$")
    assert RESERVED_HANDLES
    assert [h for h in RESERVED_HANDLES if not shape.match(h)] == []
