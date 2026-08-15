from dataclasses import dataclass

from sqlalchemy import extract, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.catalog.models import Show
from tvbf.sql_fold import folded


@dataclass(frozen=True, slots=True)
class ShowTitle:
    """What the taste payload says about a show, and what it sorts by.

    `folded_name` is carried rather than recomputed because the fold is
    `sql_fold`'s and only Postgres evaluates it (see that module) — a caller
    needing a deterministic title order has to be handed the folded form or it
    will invent a second, disagreeing one in Python.
    """

    name: str
    folded_name: str
    year: int | None


async def get_by_id(db: AsyncSession, show_id: int) -> Show | None:
    return await db.get(Show, show_id)


async def titles_for_ids(db: AsyncSession, show_ids: list[int]) -> dict[int, ShowTitle]:
    """Name, folded name and premiere year for each of `show_ids`.

    Premiere **year**, not the date: the taste payload (project spec §5.3) reports
    the year because it disambiguates a title, where the month and day are three
    tokens no reasoning step touches. A show with no `first_air_date` gets `None`
    rather than a guessed year.

    A requested id with no `catalog.show` row is simply absent from the result —
    the caller decides what an unmirrored show means, rather than this function
    inventing a title for one.
    """
    if not show_ids:
        return {}
    rows = (
        await db.execute(
            select(
                Show.id,
                Show.name,
                folded(Show.name),
                extract("year", Show.first_air_date),
            ).where(Show.id.in_(show_ids))
        )
    ).all()
    return {
        show_id: ShowTitle(
            name=name,
            folded_name=folded_name,
            year=int(year) if year is not None else None,
        )
        for show_id, name, folded_name, year in rows
    }
