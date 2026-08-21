import re
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, BeforeValidator, EmailStr, Field, field_validator

from tvbf.app.handles import RESERVED_HANDLES
from tvbf.catalog.schemas import EpisodeOut, ShowSummary

# NEU-1194. An `@` and a later dot inside one whitespace-free run, with a
# non-`@` character on each side. Deliberately narrower than "contains `@` with
# a dot after it", which would reject `@home with Tom. Really`, and deliberately
# not RFC 5322: the goal is to stop a user publishing their address, not to
# parse one. The leading `[^\s@]` is the load-bearing part — it lets a display
# name *start* with `@`, which NEU-1163 is about to make commonplace by turning
# `@handle` into a first-class concept.
_EMAIL_SHAPED = re.compile(r"[^\s@]@[^\s@]*\.[^\s@]")


def _strip_display_name(v: object) -> object:
    return v.strip() if isinstance(v, str) else v


def _reject_email_shaped(v: str) -> str:
    if _EMAIL_SHAPED.search(v):
        raise ValueError("display_name must not be an email address")
    return v


# One alias over both write sites, on `OptionalDate`'s precedent. It carries the
# strip as well as the rule: without it the two doors would disagree about the
# rule itself rather than merely about `max_length`, since `" a@b.c "` would be
# checked raw at signup and stripped at `PATCH /me`. Each class keeps its own
# `Field(...)`, so the 100/80 length split is untouched — a real inconsistency,
# but a different defect with a live-data question behind it.
DisplayName = Annotated[
    str, BeforeValidator(_strip_display_name), AfterValidator(_reject_email_shaped)
]

# NEU-1163 §1. Three to thirty characters, lowercase ASCII letters, digits and
# underscores, starting with a letter. The floor is 3 because the one- and
# two-character space is the first thing a stranger takes once registration
# opens; the ceiling is 30 because that is where a handle still fits beside a
# display name in a card caption at a 375px viewport.
_HANDLE = re.compile(r"^[a-z][a-z0-9_]{2,29}$")

# The shape §5 gives an account whose display name derives to nothing, and the
# shape `scripts/refresh_db.sh` gives every non-admin account during
# anonymisation. Refused **by pattern, not by adding it to the blocklist**: the
# blocklist is a fixed set of strings and this is a shape with 4.3 billion
# members. Left claimable, a stranger could take `@user_3f4a2b1c` and wear an
# identifier a real account either holds or recently held.
_ANON_HANDLE = re.compile(r"^user_[0-9a-f]{8}$")


def _normalise_handle(v: object) -> object:
    """Strip surrounding whitespace, strip one leading `@`, lowercase.

    `TomBoone`, `@TomBoone` and `  @tomboone ` all become `tomboone`; none is
    refused. A user who types their own name the way they capitalise it, or
    pastes a handle with the sigil they saw it printed with, gets the account
    they meant instead of a form error about a rule they had no way to know.

    Normalisation rather than refusal is what actually prevents `@TomBoone` and
    `@tomboone` both existing — the `CITEXT` column is parity with `email` and
    a guard against a future writer that reaches the table without passing
    here, and nothing rests on it (NEU-1163 §1.1).

    In the alias rather than at each door, for the reason NEU-1194 §3 gives
    about `DisplayName`: `POST /signup` and `PATCH /me/handle` must reach one
    verdict on one input, and a rule applied to the raw string at one door and
    the stripped string at the other is two rules.
    """
    if not isinstance(v, str):
        return v
    return v.strip().lstrip("@").strip().lower()


def _validate_handle(v: str) -> str:
    if not _HANDLE.match(v):
        raise ValueError("handle must be 3-30 characters of a-z, 0-9 or _, and start with a letter")
    if _ANON_HANDLE.match(v):
        raise ValueError("handle is not available")
    if v in RESERVED_HANDLES:
        raise ValueError("handle is not available")
    return v


# One alias over both write sites, `DisplayName`'s precedent one screen up.
# **Uniqueness is deliberately not one of these rules**: it needs a session, a
# Pydantic validator has none, and the answer changes between validation and
# commit anyway. It lives in the service layer and answers `409` (§6.3).
Handle = Annotated[str, BeforeValidator(_normalise_handle), AfterValidator(_validate_handle)]

MyShowsSort = Literal[
    "recent_activity",
    "name_asc",
    "name_desc",
    "added",
    "my_rating_desc",
    "my_rating_asc",
]
WatchNextSort = Literal[
    "airdate_desc", "unwatched_airdate_desc", "airdate_asc", "name_asc", "name_desc"
]
UpcomingSort = Literal["airdate_asc", "airdate_desc", "added_desc", "name_asc", "name_desc"]
WatchedSort = Literal[
    "name_asc",
    "last_watched_desc",
    "last_aired_desc",
    "premiered_asc",
    "premiered_desc",
    "first_watched_desc",
]
WatchedStatusFilter = Literal["all", "finished", "in_progress"]
WatchedStatus = Literal["finished", "in_progress"]

ActivityVerb = Literal[
    "added_show",
    "watched_episode",
    "watched_season",
    "watched_show",
    "rated_show",
    "rated_episode",
]
ActivityTargetType = Literal["show", "season", "episode"]


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: DisplayName = Field(min_length=1, max_length=100)
    handle: Handle
    invite_code: str = Field(min_length=1, max_length=128)
    # **Optional in the schema, required by the handler when verification is
    # enabled** (NEU-1160 §7). Optional keeps every existing call site working
    # unchanged; the handler is where "enabled means required" is decided,
    # because it is the only place that knows. A missing token with verification
    # on is a 400 `captcha_required`, not a 422 — `api/client.ts` renders
    # field-level 422s against form fields and there is no form field for this.
    turnstile_token: str | None = Field(default=None, max_length=2048)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class AccountDeleteRequest(BaseModel):
    password: str


class SessionSummary(BaseModel):
    id: str
    device_label: str
    ip: str | None
    last_seen_at: datetime
    created_at: datetime
    is_current: bool


class MeUpdateRequest(BaseModel):
    """Body for PATCH /me. Only carries display_name today."""

    display_name: DisplayName = Field(min_length=1, max_length=80)


class HandleUpdateRequest(BaseModel):
    """Body for `PATCH /me/handle` (NEU-1163 §6.2).

    Its own request and its own route rather than a second field on
    `MeUpdateRequest`. Widening that body to a partial update in order to carry
    a throttled field beside an unthrottled one is how a display-name save ends
    up refused by a `429` about a handle the user did not touch.
    """

    handle: Handle


class UserOut(BaseModel):
    id: UUID
    email: str
    display_name: str
    handle: str
    created_at: datetime
    email_verified_at: datetime | None = None


class AuthedUserOut(UserOut):
    csrf_token: str
    activity_feed_enabled: bool
    is_admin: bool


class AdminUserOut(BaseModel):
    id: UUID
    email: str
    display_name: str
    handle: str
    created_at: datetime
    is_admin: bool
    # The admin list is the one surface that shows moderation state (NEU-1162
    # AC 8). `UserOut` and `AuthedUserOut` deliberately do **not** gain it: a
    # disabled user cannot reach either, and a field saying so would be the
    # machine-readable confirmation §2.2 refuses to hand an abuser.
    disabled_at: datetime | None


class AdminReportUserRef(BaseModel):
    """One party to a report (NEU-1197 §3).

    No `email`. An admin who needs it has `GET /admin/users`, which already
    exposes it for every account; carrying it here widens what leaks if this
    route is ever mis-gated, for a field nobody triages on. The id is the
    identity, the display name is the label.

    `disabled_at` is a **live join** to `app.user`, not a flag stored on the
    report — it cannot drift, and clearing the flag restores the truth with no
    backfill.
    """

    id: UUID
    display_name: str
    handle: str
    disabled_at: datetime | None


class AdminReportOut(BaseModel):
    """One row of the admin report queue (NEU-1197 §3).

    **Nested refs, not flat prefixes.** `AdminUserOut` is flat because it *is*
    one user; this row is a relationship between two, and
    `reporter_id` / `reporter_display_name` / `reporter_disabled_at` at three
    fields each is where that starts reading as two structs pretending not to
    be.

    The *reporter's* `disabled_at` is carried though the ticket names only the
    reported user's: five rows under `?reported_user_id=` means five *reports*,
    not five people, and three from an account already disabled as a griefer is
    the difference between a pile-on and a campaign.

    `reason` is returned **in full and verbatim** (§3.1) — untruncated, because
    reading the reason text is the whole point, and unescaped, because escaping
    belongs to the renderer. Whatever renders this must not render it as HTML.
    """

    id: UUID
    reporter: AdminReportUserRef
    reported_user: AdminReportUserRef
    reason: str
    created_at: datetime


class AdminReportPage(BaseModel):
    """`ShowListPage`'s shape, which is the only pagination vocabulary this API
    has. `total` is load-bearing rather than decorative: without it
    `?reported_user_id=X` reports a count only while it fits under the page cap,
    so the answer degrades to "at least N" exactly when the number is alarming.
    """

    items: list[AdminReportOut]
    page: int
    per_page: int
    total: int
    total_pages: int


class AdminUserUpdateRequest(BaseModel):
    is_admin: bool


class AdminUserDisabledUpdateRequest(BaseModel):
    disabled: bool


class MePreferencesUpdate(BaseModel):
    """Body for PATCH /me/preferences. Fields are optional (partial update)."""

    activity_feed_enabled: bool | None = None


class HideFromActivityUpdate(BaseModel):
    """Body for PATCH /me/shows/{show_id}/hide-from-activity."""

    hide_from_activity: bool


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class EmailChangeRequest(BaseModel):
    new_email: EmailStr
    current_password: str


class EmailChangeConfirmRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=8, max_length=128)


class MyShowEntry(BaseModel):
    show: ShowSummary
    watched_episode_count: int
    total_episode_count: int
    aired_episode_count: int = 0
    upcoming_episode_count: int = 0
    last_aired: date | None = None
    last_watched_at: datetime | None = None
    first_watched_at: datetime | None = None
    next_episode: EpisodeOut | None = None
    added_at: datetime
    my_rating: float | None = None
    hide_from_activity: bool = False


class WatchNextEntry(BaseModel):
    show: ShowSummary
    episode: EpisodeOut
    last_watched_at: datetime | None = None
    last_aired: date | None = None
    watched_episode_count: int
    aired_episode_count: int
    upcoming_episode_count: int
    added_at: datetime | None = None


class UpcomingEntry(BaseModel):
    show: ShowSummary
    episode: EpisodeOut
    watched_episode_count: int
    aired_episode_count: int
    upcoming_episode_count: int
    added_at: datetime | None = None


class UpcomingSeasonEntry(BaseModel):
    show: ShowSummary
    season_number: int
    season_name: str | None = None
    premiere_date: date | None = None
    added_at: datetime | None = None


class UpcomingShowEntry(BaseModel):
    show: ShowSummary
    premiere_date: date | None = None
    added_at: datetime | None = None


class EpisodeWatchOut(BaseModel):
    episode_id: int
    watched_at: datetime


class WatchedEntry(BaseModel):
    """One show with watch history, for a library's Watched tab.

    **`my_rating` is the row owner's rating, not the requester's** (NEU-1191).
    The two are the same user on `GET /me/watched` and differ on
    `GET /users/{id}/watched`, which serves a friend's library: there the value
    is the friend's rating of the show, and is null when the friend has not
    rated it however the caller rated it. `MyShowEntry.my_rating` behaves
    identically, and the frontend attributes both through `ratingOwnerFor`
    (NEU-1181).

    It is a top-level field rather than `show.my_rating` deliberately:
    `my_rating` on a `ShowSummary` means the *requester's* rating everywhere it
    is filled (`BrowseShowOut`, `SimilarShowOut`, `ShowDetail`, `TrendingShow`),
    so a friend's rating behind that name would reintroduce exactly the
    confusion NEU-1181 removed from the components.
    """

    show: ShowSummary
    watched_episode_count: int
    aired_episode_count: int
    total_episode_count: int
    last_watched_at: datetime | None = None
    last_aired: date | None = None
    first_watched_at: datetime | None = None
    in_my_shows: bool
    status: WatchedStatus
    my_rating: float | None = None


class BulkSeasonResult(BaseModel):
    marked: int


class SeasonProgress(BaseModel):
    season: int
    aired: int
    watched: int


class RecommendationOut(ShowSummary):
    """One suggestion: a `ShowSummary` **flattened**, plus its rank (NEU-1112).

    Flattened rather than nested under a `show` key, unlike every other entry
    shape in this module. Those carry per-show *progress* — watched counts, a
    next episode — which is a second object with its own identity; a
    recommendation carries a position, and nesting would cost the frontend a
    wrapper type for nothing when `ShowGrid` and `ShowCard` already take a
    `ShowSummary`.

    `rank` is the model's own ordering, carried through so a client can display
    it and so "the order is the ranking" survives a client that re-serializes
    the list.

    **`reason` is deliberately not here, and it is still asked for and still
    stored.** The card has one truncated 10px line for it, which is not enough
    room for a sentence, so serving it only put model-authored prose on the wire
    for nobody to read. It stays in `app.user_recommendation.reason` and in the
    set's `raw_response`, because removing it from the *prompt* is a different
    and riskier change: `reason` is where the model puts its explanations, and a
    2026-08-17 production run showed what happens when it has nowhere to put
    them — they land in `title`, which resolves to nothing (see
    `recommendations/prompt.INSTRUCTION`). Cheap insurance at ~1,100 output
    tokens a call.
    """

    rank: int


class RecommendationsOut(BaseModel):
    """The `GET /me/recommendations` body.

    An object rather than a bare array, and an **empty list rather than a 204**:
    the frontend distinguishes "no recommendations" from "the request failed" by
    status code, and a 204 collapses the two into one thing it cannot render
    differently.
    """

    recommendations: list[RecommendationOut] = []


class RecommendationsRunRequest(BaseModel):
    """The optional body of `POST /admin/recommendations` (NEU-1110).

    `user_id` narrows the run to one account, which is the reason the endpoint
    exists: testing a prompt edit against a single user is the difference between
    an iteration loop measured in minutes and one measured in weeks. Omitting it
    — or the body entirely — runs the same pass the Sunday schedule runs.
    """

    user_id: UUID | None = None


class InviteCreateRequest(BaseModel):
    email_hint: EmailStr | None = None


class InviteOut(BaseModel):
    code: str
    email_hint: str | None
    created_at: datetime
    consumed_at: datetime | None
    consumed_by_user_id: UUID | None


ConnectionState = Literal["pending", "accepted", "blocked"]


class UserBrief(BaseModel):
    id: UUID
    display_name: str
    handle: str


class UserSearchResult(BaseModel):
    """One row of `GET /users/search`.

    Keeps `display_name` beside the handle. Returning the handle alone would be
    the strongest anti-impersonation stance available and it throws away the
    name people actually recognise; both fields side by side is what makes the
    disambiguation legible (NEU-1163 §7).
    """

    id: UUID
    display_name: str
    handle: str


class ConnectionRequestCreate(BaseModel):
    addressee_id: UUID


class ConnectionRequestOut(BaseModel):
    id: UUID
    requester: UserBrief
    addressee: UserBrief
    state: ConnectionState
    created_at: datetime
    responded_at: datetime | None


class ConnectionRequestList(BaseModel):
    incoming: list[ConnectionRequestOut]
    outgoing: list[ConnectionRequestOut]


class ConnectionOut(BaseModel):
    user: UserBrief
    since: datetime


class BlockedUserOut(BaseModel):
    user: UserBrief
    blocked_at: datetime


class ShowFriendActivity(BaseModel):
    in_my_shows: list[UserBrief]
    watched: list[UserBrief]


_VALID_STARS = {Decimal("0.5") * i for i in range(1, 11)}


class ShowRatingIn(BaseModel):
    stars: Decimal

    @field_validator("stars")
    @classmethod
    def _validate(cls, v: Decimal) -> Decimal:
        if v not in _VALID_STARS:
            raise ValueError("stars must be one of 0.5, 1.0, ..., 5.0")
        return v


class ShowRatingOut(BaseModel):
    show_id: int
    stars: float
    rated_at: datetime


class EpisodeRatingIn(ShowRatingIn):
    pass


class EpisodeRatingOut(BaseModel):
    episode_id: int
    stars: float
    rated_at: datetime


class FriendRatingItem(BaseModel):
    user_id: UUID
    display_name: str
    handle: str
    stars: float
    rated_at: datetime


class FriendRatingsResponse(BaseModel):
    avg: float | None
    count: int
    items: list[FriendRatingItem]


FeedKind = Literal[
    "added_show",
    "watched_episode",
    "watched_episode_run",
    "watched_season",
    "watched_show",
    "rated_show",
    "rated_episode",
]


class ShowMini(BaseModel):
    id: int
    name: str


class EpisodeMini(BaseModel):
    id: int
    name: str | None
    season: int
    # `None` for a copied special, which has no real episode number.
    # `EpisodeOut.number` has always been nullable for the same reason; this one
    # rendered `0` instead until NEU-1062, which is a number no episode has.
    number: int | None


class FeedItem(BaseModel):
    id: str
    actor: UserBrief
    kind: FeedKind
    show: ShowMini | None
    episode: EpisodeMini | None
    season_number: int | None
    # The season's own name, for the `watched_season` roll-up (NEU-1132). Null on
    # every other kind (they carry no `season_number`) and null when upstream
    # never named the season, so the SPA falls back to the number either way.
    season_name: str | None
    rollup_count: int | None
    stars: float | None
    occurred_at: datetime


class FeedPage(BaseModel):
    items: list[FeedItem]
    next_cursor: str | None
