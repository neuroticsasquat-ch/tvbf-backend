from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import (
    ConnectionAlreadyExists,
    ConnectionBlocked,
    SelfConnectionForbidden,
)
from tvbf.app.models import Connection, User
from tvbf.app.repos import user_repo
from tvbf.app.schemas import (
    ConnectionRequestCreate,
    ConnectionRequestOut,
    UserBrief,
)
from tvbf.app.services import connection_service
from tvbf.deps import get_current_user, get_session, require_csrf

router = APIRouter(tags=["connections"])


def _to_request_out(row: Connection, requester: User, addressee: User) -> ConnectionRequestOut:
    return ConnectionRequestOut(
        id=row.id,
        requester=UserBrief(id=requester.id, display_name=requester.display_name),
        addressee=UserBrief(id=addressee.id, display_name=addressee.display_name),
        state=row.state,  # type: ignore[arg-type]
        created_at=row.created_at,
        responded_at=row.responded_at,
    )


@router.post(
    "/connection-requests",
    response_model=ConnectionRequestOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
async def create_connection_request(
    payload: ConnectionRequestCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ConnectionRequestOut:
    addressee = await user_repo.get_by_id(db, payload.addressee_id)
    if addressee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="addressee_not_found")

    try:
        row = await connection_service.send_request(
            db, requester_id=user.id, addressee_id=addressee.id
        )
    except SelfConnectionForbidden as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="self_connection_forbidden"
        ) from err
    except (ConnectionAlreadyExists, ConnectionBlocked) as err:
        # Deliberately vague to avoid leaking whether the relationship is
        # pending, accepted, or blocked.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="connection_exists"
        ) from err

    return _to_request_out(row, requester=user, addressee=addressee)
