from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (  # noqa: I001
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    event,
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

watch_archive_record_type_enum = PGEnum(
    "show_watch",
    "episode_watch",
    "show_rating",
    "episode_rating",
    name="watch_archive_record_type",
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
    activity_feed_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("TRUE")
    )
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("FALSE"))


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
    hide_from_activity: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
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


class UserShowRating(Base):
    __tablename__ = "user_show_rating"
    __table_args__ = (
        CheckConstraint(
            "stars IN (0.5,1.0,1.5,2.0,2.5,3.0,3.5,4.0,4.5,5.0)",
            name="ck_user_show_rating_stars",
        ),
        UniqueConstraint("user_id", "show_id", name="uq_user_show_rating"),
        Index("ix_user_show_rating_user_id", "user_id"),
        Index("ix_user_show_rating_show_id", "show_id"),
        {"schema": "app"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="CASCADE"),
        nullable=False,
    )
    show_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tvmaze.show.id", ondelete="CASCADE"),
        nullable=False,
    )
    stars: Mapped[Decimal] = mapped_column(Numeric(2, 1), nullable=False)
    rated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserEpisodeRating(Base):
    __tablename__ = "user_episode_rating"
    __table_args__ = (
        CheckConstraint(
            "stars IN (0.5,1.0,1.5,2.0,2.5,3.0,3.5,4.0,4.5,5.0)",
            name="ck_user_episode_rating_stars",
        ),
        UniqueConstraint("user_id", "episode_id", name="uq_user_episode_rating"),
        Index("ix_user_episode_rating_user_id", "user_id"),
        Index("ix_user_episode_rating_episode_id", "episode_id"),
        {"schema": "app"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="CASCADE"),
        nullable=False,
    )
    episode_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tvmaze.episode.id", ondelete="CASCADE"),
        nullable=False,
    )
    stars: Mapped[Decimal] = mapped_column(Numeric(2, 1), nullable=False)
    rated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ActivityEvent(Base):
    __tablename__ = "activity_event"
    __table_args__ = (
        UniqueConstraint(
            "actor_id",
            "verb",
            "target_type",
            "target_id",
            "season_number",
            name="uq_activity_event",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_activity_event_actor_created", "actor_id", "created_at"),
        Index("ix_activity_event_target", "target_type", "target_id"),
        {"schema": "app"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    actor_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user.id", ondelete="CASCADE"),
        nullable=False,
    )
    verb: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    season_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


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


class WatchArchive(Base):
    """Source-independent snapshot of every watch and rating record (NEU-1029).

    The no-loss guarantee for the TMDB migration, decoupled from the migration's
    success. Every row describes *what was watched* in human terms — who, which
    show, which season and episode, when — rather than pointing at a catalog row
    that a later source change may delete. A catastrophic mapping failure stays
    recoverable by hand indefinitely, because reconstructing a user's history
    from this table needs no reference to `tvmaze` or `catalog` at all.

    Three properties make that hold, and each is load-bearing:

    * **Append-only.** Rows are never updated and never pruned. The snapshot
      writer is `INSERT ... ON CONFLICT DO NOTHING`; there is deliberately no
      `DO UPDATE` branch, so a re-run cannot rewrite what an earlier run
      recorded.
    * **No foreign key into any catalog schema.** `source_show_id` /
      `source_episode_id` are TV Maze ids carried for convenience — a
      cross-reference while the mirror still exists — not the row's identity.
      Nothing here breaks when `tvmaze` is eventually dropped.
    * **Human-readable identity is NOT NULL.** `show_name` always, plus
      `season_number` and an episode locator for episode-grain rows (see
      `ck_watch_archive_episode_identity`). A row that cannot say what it
      describes would be worthless as a backstop, so the table refuses one.

    The table has **no foreign keys at all** — not into `tvmaze`, and
    deliberately not into `app.user` either. A `CASCADE` to `app.user` would
    prune the archive on account deletion, and "never pruned" admits no
    exceptions: the reconciliation harness has to prove identical episode counts
    either side of cutover, and an account deleted mid-window would silently
    move that target. `user_id` is a convenience reference exactly like
    `source_show_id`; `user_email` and `user_display_name` carry the identity,
    the same way `show_name` rather than `source_show_id` identifies a show.

    Append-only is then enforced in the DDL, by the `watch_archive_no_mutation`
    trigger, rather than left to how this module happens to write: every UPDATE
    and every DELETE raises, with no carve-out. A docstring is not a guarantee
    for the one table whose job is to be the last surviving copy.

    **Account deletion therefore leaves a user's archive rows standing**, email
    and display name included. That is the ticket's instruction rather than an
    oversight, and it is the deliberate cost of the guarantee — but it means
    this table has to be dropped by hand once the migration is done.
    """

    __tablename__ = "watch_archive"
    __table_args__ = (
        UniqueConstraint(
            "record_type",
            "user_id",
            "source_show_id",
            "source_episode_id",
            name="uq_watch_archive_source_row",
            # Show-grain rows carry a NULL `source_episode_id`; under the default
            # NULLS DISTINCT they would never conflict and every re-run would
            # duplicate all 620 of them.
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "(record_type IN ('episode_watch', 'episode_rating')) "
            "= (source_episode_id IS NOT NULL)",
            name="ck_watch_archive_episode_grain",
        ),
        CheckConstraint(
            "record_type IN ('show_watch', 'show_rating') "
            "OR (season_number IS NOT NULL "
            "AND (episode_number IS NOT NULL OR episode_title IS NOT NULL))",
            name="ck_watch_archive_episode_identity",
        ),
        CheckConstraint(
            "(record_type IN ('show_rating', 'episode_rating')) = (stars IS NOT NULL)",
            name="ck_watch_archive_rating_stars",
        ),
        Index("ix_watch_archive_user_id", "user_id"),
        {"schema": "app"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    record_type: Mapped[str] = mapped_column(watch_archive_record_type_enum, nullable=False)

    # Who. Unconstrained by design (see the class docstring): the email and
    # display name are denormalised so a row names a person without joining
    # `app.user`, and so deleting that user cannot take the row with it.
    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    user_email: Mapped[str] = mapped_column(Text, nullable=False)
    user_display_name: Mapped[str] = mapped_column(Text, nullable=False)

    # What. `show_premiered_year` disambiguates the reboots and same-title
    # remakes that make a bare name insufficient; it is nullable because three
    # prod shows genuinely have no premiere date.
    show_name: Mapped[str] = mapped_column(Text, nullable=False)
    show_premiered_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    episode_airdate: Mapped[date | None] = mapped_column(Date, nullable=True)

    # When, and — for ratings — what score. `occurred_at` is the source row's own
    # timestamp: `watched_at`, `rated_at`, or the tracked-show `created_at`.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stars: Mapped[Decimal | None] = mapped_column(Numeric(2, 1), nullable=True)

    # Convenience cross-references, not identity. Deliberately unconstrained: no
    # FK into `tvmaze`, so dropping that schema leaves the archive untouched.
    source_show_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_episode_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    show_imdb_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    show_tvdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# The append-only trigger rides the table's own create event so the two ways this
# table comes into being — `alembic upgrade` in dev and prod, `create_all` in the
# test suite — produce the same object. Wiring it only into the migration would
# leave every test running against a table that quietly permits what production
# forbids, which is the one difference that must not exist here.
WATCH_ARCHIVE_NO_MUTATION_FUNCTION = DDL("""
    CREATE OR REPLACE FUNCTION app.watch_archive_no_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION
            'app.watch_archive is append-only: %% is not permitted', TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$
""")

WATCH_ARCHIVE_NO_MUTATION_TRIGGER = DDL("""
    CREATE OR REPLACE TRIGGER watch_archive_no_mutation
    BEFORE UPDATE OR DELETE ON app.watch_archive
    FOR EACH ROW EXECUTE FUNCTION app.watch_archive_no_mutation()
""")

event.listen(WatchArchive.__table__, "after_create", WATCH_ARCHIVE_NO_MUTATION_FUNCTION)
event.listen(WatchArchive.__table__, "after_create", WATCH_ARCHIVE_NO_MUTATION_TRIGGER)
