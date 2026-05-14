from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

from tvbf.tvmaze.schemas import EpisodeOut, ShowSummary

MyShowsSort = Literal["recent_activity", "name_asc", "name_desc", "added"]
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


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=100)
    invite_code: str = Field(min_length=1, max_length=128)


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

    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("display_name", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


class UserOut(BaseModel):
    id: UUID
    email: str
    display_name: str
    created_at: datetime
    email_verified_at: datetime | None = None


class AuthedUserOut(UserOut):
    csrf_token: str


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
    show: ShowSummary
    watched_episode_count: int
    aired_episode_count: int
    total_episode_count: int
    last_watched_at: datetime | None = None
    last_aired: date | None = None
    first_watched_at: datetime | None = None
    in_my_shows: bool
    status: WatchedStatus


class BulkSeasonResult(BaseModel):
    marked: int


class SeasonProgress(BaseModel):
    season: int
    aired: int
    watched: int


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


class UserSearchResult(BaseModel):
    id: UUID
    display_name: str


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
    stars: float
    rated_at: datetime


class FriendRatingsResponse(BaseModel):
    avg: float | None
    count: int
    items: list[FriendRatingItem]
