from datetime import datetime
from uuid import UUID

from sqlalchemy import (  # noqa: I001
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET, JSONB
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from tvbf.db import Base

connection_state_enum = PGEnum(
    "pending",
    "accepted",
    "blocked",
    name="connection_state",
    schema="app",
)

auth_token_purpose_enum = PGEnum(
    "email_verification",
    "password_reset",
    "email_change",
    name="auth_token_purpose",
    schema="app",
)


class User(Base):
    __tablename__ = "user"
    __table_args__ = {"schema": "app"}

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Session(Base):
    __tablename__ = "session"
    __table_args__ = (
        Index("ix_session_user_id", "user_id"),
        Index("ix_session_expires_at", "expires_at"),
        {"schema": "app"},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)


class UserShowWatch(Base):
    __tablename__ = "user_show_watch"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "show_id"),
        {"schema": "app"},
    )

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="CASCADE"),
        nullable=False,
    )
    show_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tvmaze.show.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class UserEpisodeWatch(Base):
    __tablename__ = "user_episode_watch"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "episode_id"),
        {"schema": "app"},
    )

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="CASCADE"),
        nullable=False,
    )
    episode_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tvmaze.episode.id", ondelete="CASCADE"),
        nullable=False,
    )
    watched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class LoginAttempt(Base):
    __tablename__ = "login_attempt"
    __table_args__ = (
        Index("ix_login_attempt_email_at", "email", "attempted_at"),
        {"schema": "app"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)


class Invite(Base):
    __tablename__ = "invite"
    __table_args__ = {"schema": "app"}

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    email_hint: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_by_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="SET NULL"),
        nullable=True,
    )


class Connection(Base):
    __tablename__ = "connection"
    __table_args__ = (
        CheckConstraint(
            "requester_id <> addressee_id",
            name="ck_connection_not_self",
        ),
        Index(
            "uq_connection_unordered_pair",
            func.least(text("requester_id"), text("addressee_id")),
            func.greatest(text("requester_id"), text("addressee_id")),
            unique=True,
        ),
        Index("ix_connection_requester_state", "requester_id", "state"),
        Index("ix_connection_addressee_state", "addressee_id", "state"),
        {"schema": "app"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    requester_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="CASCADE"),
        nullable=False,
    )
    addressee_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(connection_state_enum, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuthToken(Base):
    __tablename__ = "auth_token"
    __table_args__ = (
        Index("ix_auth_token_token_hash", "token_hash"),
        Index("ix_auth_token_user_purpose_created", "user_id", "purpose", "created_at"),
        {"schema": "app"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(auth_token_purpose_enum, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
