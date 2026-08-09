from datetime import datetime

from sqlalchemy import DateTime, Double, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from tvbf.db import Base

SCHEMA = "catalog"


class RateBudget(Base):
    """One upstream's request budget, as a token bucket every process shares.

    An upstream's cap applies to us as a whole. An in-process limiter could
    express that only while every job ran inside the app; the daily now runs as
    its own process (`tvbf.jobs.daily_update`), so the budget has to live
    somewhere both can see (ADR-0006).

    A row per source, unlike `tvmaze.rate_budget`'s single check-constrained
    row: TMDB's ceiling is its own and has nothing to do with TV Maze's
    (NEU-1027). TV Maze keeps its original row until cutover — migrating a live
    token bucket mid-ingest buys nothing.
    """

    __tablename__ = "rate_budget"
    __table_args__ = ({"schema": SCHEMA},)

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    # Fractional by design — refill is `elapsed × rate`, which lands mid-token
    # far more often than not.
    tokens: Mapped[float] = mapped_column(Double, nullable=False)
    # The default only ever stamps a seed row. Every write from the limiter
    # uses `clock_timestamp()`, never `now()`: `now()` is transaction-start
    # time, so an acquirer that waited on the row lock would measure elapsed
    # time from before it waited and over-refill.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
