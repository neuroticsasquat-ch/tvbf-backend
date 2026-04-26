from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.tvmaze.models import Show


async def get_by_id(db: AsyncSession, show_id: int) -> Show | None:
    return await db.get(Show, show_id)
