from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import User
from tvbf.app.repos import connection_repo, user_repo
from tvbf.app.schemas import UserSearchResult
from tvbf.deps import get_current_user, get_session

router = APIRouter(tags=["users"])

SEARCH_LIMIT = 20
MIN_QUERY_LENGTH = 2


@router.get("/users/search", response_model=list[UserSearchResult])
async def search_users(
    q: Annotated[str, Query()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[UserSearchResult]:
    if len(q) < MIN_QUERY_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="query_too_short",
        )

    blocked_ids = await connection_repo.list_blocked_user_ids(db, user.id)
    excluded = blocked_ids | {user.id}
    rows = await user_repo.search(db, query=q, limit=SEARCH_LIMIT, exclude_ids=excluded)
    return [UserSearchResult(id=row.id, display_name=row.display_name) for row in rows]
