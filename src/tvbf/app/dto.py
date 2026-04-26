from datetime import datetime
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


class UpcomingEntry(BaseModel):
    show: ShowSummary
    episode: EpisodeOut


class EpisodeWatchOut(BaseModel):
    episode_id: int
    watched_at: datetime


class BulkSeasonResult(BaseModel):
    marked: int
