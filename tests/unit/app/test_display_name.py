"""The display-name rule (NEU-1194), asserted against both write sites at once.

`DisplayName` is one `Annotated` alias shared by `SignupRequest` and
`MeUpdateRequest`, so every case below is parametrised over both classes: the
whole point of the alias is that one input gets one verdict at both doors. A
test naming only one of them would pass while the two disagreed.

The accept list is as load-bearing as the reject list. `tom@localhost` and
`Tom @home.com` are documented accepts (spec §2) rather than oversights — the
rule stops a routable address being published, it does not parse one — and
`@home with Tom` must keep working because NEU-1163 is making `@handle` a
first-class concept one ticket over.
"""

import pytest
from pydantic import ValidationError

from tvbf.app.schemas import MeUpdateRequest, SignupRequest

_SIGNUP_REST = {
    "email": "someone@example.com",
    "password": "correct horse battery",
    "handle": "someone_here",
    "invite_code": "invite-code",
}


def _validate(model: type, value: str) -> str:
    if model is SignupRequest:
        return SignupRequest(display_name=value, **_SIGNUP_REST).display_name
    return MeUpdateRequest(display_name=value).display_name


BOTH = pytest.mark.parametrize("model", [SignupRequest, MeUpdateRequest])


@BOTH
@pytest.mark.parametrize(
    "value",
    [
        "jeanne_briggs@yahoo.com",
        "a@b.c",
        " a@b.c ",  # the strip runs before the rule, at both doors
        "Ask me at tom.boone@example.co.uk sometime",
    ],
)
def test_an_email_shaped_display_name_is_rejected(model: type, value: str) -> None:
    with pytest.raises(ValidationError):
        _validate(model, value)


@BOTH
@pytest.mark.parametrize(
    "value",
    [
        "@home with Tom",
        "@home with Tom. Really",
        "Tom O'Brien @ 3.5 stars",
        "tom@localhost",  # no dot — not a routable address
        "Tom @home.com",  # no local part in front of the @
        "Jeanne Briggs",
    ],
)
def test_a_display_name_that_is_not_an_address_is_accepted(model: type, value: str) -> None:
    assert _validate(model, value) == value


@BOTH
def test_whitespace_is_stripped_at_both_write_sites(model: type) -> None:
    assert _validate(model, "   Alice   ") == "Alice"


@BOTH
def test_a_whitespace_only_display_name_is_rejected_at_both_write_sites(model: type) -> None:
    with pytest.raises(ValidationError):
        _validate(model, "   ")


def test_signup_still_accepts_a_hundred_characters() -> None:
    assert _validate(SignupRequest, "a" * 100) == "a" * 100
    with pytest.raises(ValidationError):
        _validate(SignupRequest, "a" * 101)


def test_patch_me_still_caps_at_eighty_characters() -> None:
    assert _validate(MeUpdateRequest, "a" * 80) == "a" * 80
    with pytest.raises(ValidationError):
        _validate(MeUpdateRequest, "a" * 81)
