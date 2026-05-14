from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
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


class IngestRun(Base):
    __tablename__ = "ingest_run"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('initial', 'update', 'akas_backfill')",
            name="ck_ingest_run_kind",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'cancelled')",
            name="ck_ingest_run_status",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
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
