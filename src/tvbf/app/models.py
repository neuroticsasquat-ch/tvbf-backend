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
        ForeignKey("catalog.show.id", ondelete="CASCADE", name="fk_usw_show"),
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
        ForeignKey("catalog.episode.id", ondelete="CASCADE", name="fk_uew_episode"),
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
        ForeignKey("catalog.show.id", ondelete="CASCADE", name="fk_user_show_rating_show"),
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
        ForeignKey(
            "catalog.episode.id",
            ondelete="CASCADE",
            name="fk_user_episode_rating_episode",
        ),
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
    from this table needs no reference to a catalog schema at all.

    Three properties make that hold, and each is load-bearing:

    * **Append-only.** Rows are never updated and never pruned. The snapshot
      writer is `INSERT ... ON CONFLICT DO NOTHING`; there is deliberately no
      `DO UPDATE` branch, so a re-run cannot rewrite what an earlier run
      recorded.
    * **No foreign key into any catalog schema.** `source_show_id` /
      `source_episode_id` are carried for convenience — a cross-reference, not
      the row's identity. They were written as TV Maze ids and still resolve
      against `catalog`, because NEU-1042 preserved those ids as the catalog
      surrogates. NEU-1051 dropping `tvmaze` broke nothing here, which was the
      whole point of the constraint being absent.
    * **Human-readable identity is NOT NULL.** `show_name` always, plus
      `season_number` and an episode locator for episode-grain rows (see
      `ck_watch_archive_episode_identity`). A row that cannot say what it
      describes would be worthless as a backstop, so the table refuses one.

    The table has **no foreign keys at all** — not into a catalog schema, and
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

    # Convenience cross-references, not identity. Deliberately unconstrained,
    # which is why dropping `tvmaze` in NEU-1051 left the archive untouched.
    source_show_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_episode_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    show_imdb_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    show_tvdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# The four terminal states one weekly-pass attempt for one user can end in
# (project spec §9). Three of them look identical from outside the database —
# never ran, ran and resolved nothing, ran and failed — and at 3-5 users nobody
# is going to file a bug, so the set row is the only place a failure becomes
# visible at all.
#
# The vocabulary is ours rather than an upstream value, so a CHECK constraint is
# the right guard, on `ck_show_match_method`'s precedent: a typo'd status is a
# bug in our own writer, not a value somebody else changed on us.
SET_STATUS_SUCCEEDED = "succeeded"
SET_STATUS_FAILED = "failed"
SET_STATUS_NO_MATCHES = "no_matches"
SET_STATUS_INSUFFICIENT_HISTORY = "insufficient_history"

RECOMMENDATION_SET_STATUSES: tuple[str, ...] = (
    SET_STATUS_SUCCEEDED,
    SET_STATUS_FAILED,
    SET_STATUS_NO_MATCHES,
    SET_STATUS_INSUFFICIENT_HISTORY,
)

# Which resolution tier turned a model-authored title + year into a
# `catalog.show` surrogate id (project spec §8): the show's own folded name, or
# one of its AKAs. It ports NEU-1043's `match_method` for the reason that column
# exists there — it makes one tier retractable as a batch, a `WHERE` clause
# rather than a re-run of every user.
MATCHED_VIA_NAME = "name"
MATCHED_VIA_AKA = "aka"

RECOMMENDATION_MATCHED_VIA: tuple[str, ...] = (MATCHED_VIA_NAME, MATCHED_VIA_AKA)


def _one_of(column: str, values: tuple[str, ...]) -> str:
    """Render `col IN ('a', 'b')` from the tuple above.

    It keeps the constraint `create_all` builds and the constant the writers
    import in step with each other. **The migration holds the same list written
    out**, on the `watch_archive_no_mutation` trigger's terms: declared twice on
    purpose, so a value added here has to be added there too, in the same commit
    and as an `ALTER ... DROP CONSTRAINT` / `ADD CONSTRAINT` pair. Nothing can
    check that for you — the test suite builds these tables from the models and
    never sees the migration's copy.
    """
    return "{} IN ({})".format(column, ", ".join(f"'{value}'" for value in values))


class UserRecommendationSet(Base):
    """One generated batch of recommendations for one user.

    **This row is what makes the weekly swap atomic and non-destructive**
    (project spec §9): the pass inserts a new set with its rows, and the
    previous set simply stops being the newest. Nothing is deleted ahead of a
    write that might fail, so a provider outage at 4am on Sunday leaves last
    week's recommendations standing rather than blanking the section — which is
    the single most important property of this table. A set is superseded,
    never mutated; reads take the newest row per user carrying
    `status = 'succeeded'`.

    It is also the run record. There is deliberately no `ingest_run` row and no
    run table of any kind for this job (project spec §10): a set already carries
    status, timing, tokens and the raw response per user, and a second home for
    run rows would mean a second stale-run cleanup, a second liveness guard and
    a second status route for a job that finishes in seconds.

    `compiled_payload` and `raw_response` together are how a bad recommendation
    gets diagnosed — what the model was told *and* what it said. ~12KB for the
    heaviest user and ~52 rows per user per year, so nothing prunes them. That
    does put a second copy of the user's watch history here, which is why
    `user_id` cascades: it is what keeps account deletion complete, deliberately
    unlike `watch_archive` above, which carries no foreign key at all.
    """

    __tablename__ = "user_recommendation_set"
    __table_args__ = (
        CheckConstraint(
            _one_of("status", RECOMMENDATION_SET_STATUSES),
            name="ck_user_recommendation_set_status",
        ),
        # Every read of this table is "the newest rows for one user", which the
        # spec's own is: reads take the newest row per user carrying
        # `status = 'succeeded'` (§9). Postgres scans a btree backwards, so one
        # ascending index serves that without a DESC term, and the status filter
        # falls out of a handful of rows rather than needing a term of its own —
        # a user accrues ~52 sets a year.
        #
        # NEU-1108 settled which set the regeneration gate compares its hash
        # against, in `app/repos/recommendation_repo.py`: the newest `succeeded`
        # one, the same set the API serves. It was not free either way — a
        # `failed` set carries a `payload_hash` like any other, so a gate reading
        # the newest set of *any* status would skip an unchanged user forever
        # after one provider outage. That query orders on `id` behind
        # `generated_at` to break the tie two sets written in one transaction
        # produce, which this index does not cover; at ~52 rows a year the sort
        # is over a handful of rows either way.
        Index("ix_user_recommendation_set_user_generated", "user_id", "generated_at"),
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
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # The regeneration gate (project spec §9.1) compares this against the hash of
    # the payload it just compiled; identical means skip the call. It covers the
    # payload, `prompt_version` and `model` — which is why those two are stored
    # beside it rather than inferred from whatever the code holds today, since a
    # prompt edit has to re-run everybody exactly once and that is only
    # observable against the version the set was generated under.
    #
    # NOT NULL at every status, because all four are reached *after* the payload
    # is compiled: `insufficient_history` from the floor, `failed` from the call,
    # `no_matches` from resolution, `succeeded` from a set that resolved.
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    # Cost recorded at the point of spend, which is what makes "does this scale"
    # answerable at all. Nullable because a call that never returned reports no
    # usage, and one that was never made spends nothing.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    compiled_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Nullable for the same reason the token counts are: there is no response to
    # record when the provider failed or was never asked.
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class UserRecommendation(Base):
    """One resolved suggestion inside a set.

    The pass asks the model for 25 and the surface displays 12 (project spec
    §7): the headroom absorbs resolution failures, the never-recommend filter,
    and the tombstone/adult filtering that happens at *read* time, since a set
    generated in March can name a show tombstoned in June.

    `reason` is model-authored prose. It is **written and never served**: the
    card has one truncated 10px line for it, which is not room for a sentence,
    so `RecommendationOut` stopped carrying it. It stays here because it is
    where the model puts its explanations, and taking that away from the prompt
    is what pushes them into `title` — see `recommendations/prompt.INSTRUCTION`
    for the run that proved it. Treat the stored value as diagnostic, not as
    content: it can assert things about a show that are untrue, and nothing
    renders it.
    """

    __tablename__ = "user_recommendation"
    __table_args__ = (
        CheckConstraint(
            _one_of("matched_via", RECOMMENDATION_MATCHED_VIA),
            name="ck_user_recommendation_matched_via",
        ),
        # The model's own ordering is the only ordering there is, so a set holds
        # each rank once. Its leading column is also the index every read of a
        # set uses, so `set_id` needs no index of its own.
        UniqueConstraint("set_id", "rank", name="uq_user_recommendation_set_rank"),
        # Not for reading. `show_id` is an `ON DELETE CASCADE` FK into
        # `catalog.show`, and Postgres has to find the referencing rows every
        # time a show is deleted — the tombstone pass and `orphan_retire` both
        # do that in bulk. `ix_user_show_rating_show_id` exists for the same
        # reason.
        Index("ix_user_recommendation_show_id", "show_id"),
        {"schema": "app"},
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    set_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.user_recommendation_set.id", ondelete="CASCADE"),
        nullable=False,
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    # Named explicitly, like every other `app` -> `catalog` foreign key, because
    # the test suite builds these tables with `create_all` and production builds
    # them with Alembic, and the two have to agree on the constraint's name.
    show_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("catalog.show.id", ondelete="CASCADE", name="fk_user_recommendation_show"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    matched_via: Mapped[str] = mapped_column(Text, nullable=False)
    recovered_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The raw dressed title this row was recovered from, or NULL (NEU-1173).

    The model intermittently answers with a comparison rather than a bare title —
    `"The Leftovers' 'Manhunt: Unabomber'"` — and the pass reads the recommendation
    back out of the quotes (`recommendations/prompt.quoted_candidate`). The raw
    form is stored rather than the extracted candidate because the candidate is
    re-derivable from it and the raw form is the thing that is otherwise lost once
    the container logs rotate.

    **Not folded into `matched_via`.** That column answers "which catalog tier
    matched" and its vocabulary stays `name` / `aka`; a recovered title still
    matched via one of those. Recovery is a property of the *title*, not of the
    tier, and one column carrying two orthogonal facts is the shape that goes
    stale. Kept separate, both stay retractable as a batch — `WHERE recovered_from
    IS NOT NULL` — which is the stated reason `matched_via` exists at all.

    Diagnostic, like `matched_via`: `GET /me/recommendations` does not serve it.
    """


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
