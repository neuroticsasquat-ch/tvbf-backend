from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from tvbf.tvmaze.schemas import EpisodeOut, ShowSummary

MyShowsSort = Literal["recent_activity", "name_asc", "name_desc", "added"]
WatchNextSort = Literal[
    "airdate_desc", "unwatched_airdate_desc", "airdate_asc", "name_asc", "name_desc"
]
UpcomingSort = Literal["airdate_asc", "airdate_desc", "added_desc", "name_asc", "name_desc"]


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


class UserOut(BaseModel):
    id: UUID
    email: str
    display_name: str
    created_at: datetime


class AuthedUserOut(UserOut):
    csrf_token: str


class MyShowEntry(BaseModel):
    show: ShowSummary
    watched_episode_count: int
    total_episode_count: int
    aired_episode_count: int = 0
    upcoming_episode_count: int = 0
    last_aired: date | None = None
    last_watched_at: datetime | None = None
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


class EpisodeWatchOut(BaseModel):
    episode_id: int
    watched_at: datetime


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
