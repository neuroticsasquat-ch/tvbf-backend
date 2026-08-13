from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from tvbf.db import Base

SCHEMA = "tvmaze"


class Network(Base):
    __tablename__ = "network"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(Text)
    country_name: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)


class WebChannel(Base):
    __tablename__ = "web_channel"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(Text)
    country_name: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)


class Genre(Base):
    __tablename__ = "genre"
    __table_args__ = (
        UniqueConstraint("name", name="uq_genre_name"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class Show(Base):
    __tablename__ = "show"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    runtime: Mapped[int | None] = mapped_column(Integer)
    premiered: Mapped[date | None] = mapped_column(Date)
    ended: Mapped[date | None] = mapped_column(Date)
    official_site: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    image_medium: Mapped[str | None] = mapped_column(Text)
    image_original: Mapped[str | None] = mapped_column(Text)
    externals_imdb: Mapped[str | None] = mapped_column(Text)
    externals_tvdb: Mapped[int | None] = mapped_column(Integer)
    externals_tvrage: Mapped[int | None] = mapped_column(Integer)
    network_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.network.id"), nullable=True
    )
    web_channel_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.web_channel.id"), nullable=True
    )
    tvmaze_updated: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    akas_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rating_average: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))
    ratings_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credits_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the show stops appearing in /updates/shows, i.e. TV Maze has
    # deleted it. The row is never removed: app.user_show_watch and
    # app.user_show_rating cascade from here, so a delete would destroy user
    # data that nothing upstream could restore (ADR-0005).
    deleted_upstream_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ShowAka(Base):
    __tablename__ = "show_aka"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    show_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.show.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(Text)
    country_name: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)


class Season(Base):
    __tablename__ = "season"
    __table_args__ = {"schema": SCHEMA}
    # No UNIQUE(show_id, number): TV Maze occasionally returns multiple seasons
    # with the same number for one show (data quirk on long-running programs).

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    show_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.show.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    episode_order: Mapped[int | None] = mapped_column(Integer)
    premiere_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    network_id: Mapped[int | None] = mapped_column(ForeignKey(f"{SCHEMA}.network.id"))
    web_channel_id: Mapped[int | None] = mapped_column(ForeignKey(f"{SCHEMA}.web_channel.id"))
    image_medium: Mapped[str | None] = mapped_column(Text)
    image_original: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    # Episode-credit pass watermark. Absence of credit rows cannot stand in for
    # "not yet fetched": 22.5% of episodes carry no crew credits and a whole
    # season legitimately may have none. See ADR-0003.
    credits_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Episode(Base):
    __tablename__ = "episode"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    show_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.show.id", ondelete="CASCADE"), nullable=False
    )
    season_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.season.id", ondelete="SET NULL")
    )
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    number: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(Text)
    airdate: Mapped[date | None] = mapped_column(Date)
    airtime: Mapped[time | None] = mapped_column(Time)
    runtime: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(Text)
    image_medium: Mapped[str | None] = mapped_column(Text)
    image_original: Mapped[str | None] = mapped_column(Text)
    rating_average: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))


class ShowGenre(Base):
    __tablename__ = "show_genre"
    __table_args__ = (
        PrimaryKeyConstraint("show_id", "genre_id", name="pk_show_genre"),
        {"schema": SCHEMA},
    )

    show_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.show.id", ondelete="CASCADE"), nullable=False
    )
    genre_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.genre.id"), nullable=False)


class Person(Base):
    __tablename__ = "person"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(Text)
    country_name: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)
    birthday: Mapped[date | None] = mapped_column(Date)
    deathday: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(Text)
    image_medium: Mapped[str | None] = mapped_column(Text)
    image_original: Mapped[str | None] = mapped_column(Text)
    tvmaze_updated: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # No credits watermark here, unlike `show` and `season`: person rows carry no
    # credits of their own since ADR-0003, and the column that used to sequence
    # the retired initial pass went with it (NEU-962).


class Character(Base):
    __tablename__ = "character"
    __table_args__ = {"schema": SCHEMA}
    # No show_id: upstream provides none (/characters/{id} has no show link),
    # and the character->show relationship is carried by the credit rows.
    # A character is not owned by one person — The Simpsons credits both
    # Hank Azaria and Harry Shearer as Carl Carlson.

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    image_medium: Mapped[str | None] = mapped_column(Text)
    image_original: Mapped[str | None] = mapped_column(Text)


class CrewRole(Base):
    __tablename__ = "crew_role"
    __table_args__ = (
        UniqueConstraint("name", name="uq_crew_role_name"),
        {"schema": SCHEMA},
    )
    # Upstream sends crew type as a bare string with no id, exactly like genre.
    # Modeled on Genre: local autoincrement id, unique name.

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class ShowCast(Base):
    __tablename__ = "show_cast"
    __table_args__ = (
        Index("ix_show_cast_show_id_sort", "show_id", "sort_order"),
        Index("ix_show_cast_person_id", "person_id"),
        {"schema": SCHEMA},
    )
    # No UNIQUE(show_id, person_id, character_id) — deliberate. Refresh is
    # delete-then-insert, so there is nothing to conflict on, and a uniqueness
    # assumption over upstream data is what broke ingestion on tvmaze.season.
    # sort_order preserves upstream billing order.

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    show_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.show.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.person.id"), nullable=False)
    character_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.character.id"), nullable=False)
    is_self: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    is_voice: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class ShowCrew(Base):
    __tablename__ = "show_crew"
    __table_args__ = (
        Index("ix_show_crew_show_id_sort", "show_id", "sort_order"),
        Index("ix_show_crew_person_id", "person_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    show_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.show.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.person.id"), nullable=False)
    role_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.crew_role.id"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class EpisodeGuestCast(Base):
    __tablename__ = "episode_guest_cast"
    __table_args__ = (
        UniqueConstraint(
            "episode_id",
            "person_id",
            "character_id",
            name="uq_egc_episode_person_character",
        ),
        Index("ix_egc_episode_id_sort", "episode_id", "sort_order"),
        Index("ix_egc_person_id", "person_id"),
        {"schema": SCHEMA},
    )
    # Still written by the person axis today; ownership moves to the season
    # fetch, whose refresh grain is one season's episodes at a time (ADR-0003).
    # That single writer is what makes a unique key possible. It has to be
    # three-part: one character is played by more than one person on 17 of
    # 1,043 sampled episodes, so (episode_id, character_id) would silently drop
    # legitimate rows.

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.episode.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.person.id"), nullable=False)
    character_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.character.id"), nullable=False)
    is_self: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    is_voice: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class EpisodeCrewRole(Base):
    __tablename__ = "episode_crew_role"
    __table_args__ = (
        UniqueConstraint("name", name="uq_episode_crew_role_name"),
        {"schema": SCHEMA},
    )
    # Kept separate from crew_role deliberately: the vocabularies are disjoint.
    # Episode-level values are Writer, Director, Story, Teleplay; none of them
    # appear among crew_role's 233 production-function names. See ADR-0003.

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class EpisodeCrew(Base):
    __tablename__ = "episode_crew"
    __table_args__ = (
        UniqueConstraint(
            "episode_id", "person_id", "role_id", name="uq_episode_crew_episode_person_role"
        ),
        Index("ix_episode_crew_episode_id_sort", "episode_id", "sort_order"),
        Index("ix_episode_crew_person_id", "person_id"),
        {"schema": SCHEMA},
    )
    # Three-part key for the same reason as episode_guest_cast: one person holds
    # more than one crew role on 36 of 1,043 sampled episodes.

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.episode.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[int] = mapped_column(ForeignKey(f"{SCHEMA}.person.id"), nullable=False)
    role_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.episode_crew_role.id"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class IngestRun(Base):
    __tablename__ = "ingest_run"
    __table_args__ = (
        CheckConstraint(
            # `person_initial` was dropped in NEU-962. Historical rows of that
            # kind survive in prod: the migration re-adds this constraint NOT
            # VALID (unconditionally — every migrated database gets it that way)
            # so the cancelled pass-C run stays readable while no new one can be
            # written. NOT VALID skips only the scan of existing rows; writes are
            # enforced either way, so what this declaration says is what every
            # database does. Tests build from `create_all` and never see the
            # migration, so they get an ordinary validated constraint from here.
            # `catalog_initial` is the TMDB full-catalog ingest (NEU-1034) and
            # `catalog_update` its daily delta (NEU-1035). Both run against
            # `catalog`, not this schema, and share this table anyway: run rows
            # are operational metadata rather than catalog data, and a second
            # copy of them would mean a second stale-run cleanup, a second
            # liveness guard and a second status route for no gain. Relocating
            # them is NEU-1050's, alongside everything else this schema still
            # holds — nothing drops `tvmaze` before then.
            "kind IN ('initial', 'update', 'akas_backfill', 'ratings_backfill', "
            "'show_refresh', 'person_update', 'episode_credits_backfill', "
            "'catalog_initial', 'catalog_update')",
            name="ck_ingest_run_kind",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled')",
            name="ck_ingest_run_status",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_update_cursor: Mapped[int | None] = mapped_column(BigInteger)
    shows_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shows_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class RateBudget(Base):
    """The TV Maze request budget, as one token bucket every process shares.

    TV Maze's cap applied to us as a whole. An in-process limiter could express
    that only while every job ran inside the app; a delta runs as its own
    process, so the budget had to live somewhere both could see (ADR-0006).

    Nothing spends from this row since NEU-1050 retired the TV Maze client —
    `rate_budget.BUCKETS` no longer registers it. The table stands with the rest
    of the schema until NEU-1051 drops it.

    One row, and the check constraint says so: a second row would be a second
    budget, which is the failure this exists to prevent.

    Every budget added since is a row in `catalog.rate_budget`, keyed by source
    (NEU-1027). This one stays where it is until cutover: migrating a live
    token bucket mid-ingest buys nothing.
    """

    __tablename__ = "rate_budget"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_rate_budget_single_row"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    # Fractional by design — refill is `elapsed × rate`, which lands mid-token
    # far more often than not.
    tokens: Mapped[float] = mapped_column(Double, nullable=False)
    # The default only ever stamps the seed row. Every write from the limiter
    # uses `clock_timestamp()`, never `now()`: `now()` is transaction-start
    # time, so an acquirer that waited on the row lock would measure elapsed
    # time from before it waited and over-refill.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
