from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from tvbf.tvmaze.dto import EpisodeOut, ShowSummary

MyShowsSort = Literal["recent_activity", "name_asc", "name_desc", "added"]
WatchNextSort = Literal["airdate_desc", "airdate_asc", "name_asc", "name_desc"]
UpcomingSort = Literal["airdate_asc", "airdate_desc", "name_asc", "name_desc"]


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
    next_episode: EpisodeOut | None = None
    added_at: datetime


class WatchNextEntry(BaseModel):
    show: ShowSummary
    episode: EpisodeOut
    last_watched_at: datetime | None = None
    last_aired: date | None = None


class UpcomingEntry(BaseModel):
    show: ShowSummary
    episode: EpisodeOut


class EpisodeWatchOut(BaseModel):
    episode_id: int
    watched_at: datetime


class BulkSeasonResult(BaseModel):
    marked: int


class InviteCreateRequest(BaseModel):
    email_hint: EmailStr | None = None


class InviteOut(BaseModel):
    code: str
    email_hint: str | None
    created_at: datetime
    consumed_at: datetime | None
    consumed_by_user_id: UUID | None
