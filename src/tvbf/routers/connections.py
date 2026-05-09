from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import (
    ConnectionAlreadyExists,
    ConnectionBlocked,
    ConnectionWrongState,
    NotAConnectionParty,
    NotFound,
    SelfConnectionForbidden,
)
from tvbf.app.models import Connection, User
from tvbf.app.repos import connection_repo, user_repo
from tvbf.app.schemas import (
    BlockedUserOut,
    ConnectionOut,
    ConnectionRequestCreate,
    ConnectionRequestList,
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


@router.get("/me/connection-requests", response_model=ConnectionRequestList)
async def list_connection_requests(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ConnectionRequestList:
    incoming, outgoing = await connection_repo.list_pending_for_user(db, user.id)

    user_ids: set[UUID] = set()
    for row in (*incoming, *outgoing):
        user_ids.add(row.requester_id)
        user_ids.add(row.addressee_id)
    users = await user_repo.get_many_by_ids(db, user_ids)

    def _hydrate(row: Connection) -> ConnectionRequestOut:
        return _to_request_out(
            row,
            requester=users[row.requester_id],
            addressee=users[row.addressee_id],
        )

    return ConnectionRequestList(
        incoming=[_hydrate(row) for row in incoming],
        outgoing=[_hydrate(row) for row in outgoing],
    )


@router.post(
    "/connection-requests/{connection_id}/accept",
    response_model=ConnectionRequestOut,
    dependencies=[Depends(require_csrf)],
)
async def accept_connection_request(
    connection_id: UUID = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> ConnectionRequestOut:
    try:
        row = await connection_service.accept(db, id=connection_id, accepting_user_id=user.id)
    except NotFound as err:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found") from err
    except NotAConnectionParty as err:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not_addressee") from err
    except ConnectionWrongState as err:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="wrong_state") from err

    users = await user_repo.get_many_by_ids(db, {row.requester_id, row.addressee_id})
    return _to_request_out(
        row, requester=users[row.requester_id], addressee=users[row.addressee_id]
    )


@router.delete(
    "/connection-requests/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def delete_connection_request(
    connection_id: UUID = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    try:
        await connection_service.delete_pending_request(db, id=connection_id, caller_id=user.id)
    except NotFound as err:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found") from err
    except NotAConnectionParty as err:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not_a_party") from err
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me/connections", response_model=list[ConnectionOut])
async def list_connections(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[ConnectionOut]:
    pairs = await connection_repo.list_accepted_for_user(db, user.id)
    other_ids = {other_id for _, other_id in pairs}
    others = await user_repo.get_many_by_ids(db, other_ids)
    out = [
        ConnectionOut(
            user=UserBrief(id=others[other_id].id, display_name=others[other_id].display_name),
            since=row.responded_at or row.created_at,
        )
        for row, other_id in pairs
        if other_id in others
    ]
    out.sort(key=lambda c: c.user.display_name)
    return out


@router.delete(
    "/me/connections/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def remove_connection(
    user_id: UUID = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    try:
        await connection_service.remove_connection(db, user_a=user.id, user_b=user_id)
    except NotFound as err:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_connected") from err
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/me/blocks/{user_id}",
    response_model=BlockedUserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
async def block_user(
    user_id: UUID = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> BlockedUserOut:
    target = await user_repo.get_by_id(db, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found")

    try:
        row = await connection_service.block(db, blocker_id=user.id, blocked_id=target.id)
    except SelfConnectionForbidden as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="self_block_forbidden"
        ) from err

    return BlockedUserOut(
        user=UserBrief(id=target.id, display_name=target.display_name),
        blocked_at=row.responded_at or row.created_at,
    )


@router.delete(
    "/me/blocks/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def unblock_user(
    user_id: UUID = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    try:
        await connection_service.unblock(db, blocker_id=user.id, blocked_id=user_id)
    except NotFound as err:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_blocked") from err
    except NotAConnectionParty as err:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not_blocker") from err
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me/blocks", response_model=list[BlockedUserOut])
async def list_blocks(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[BlockedUserOut]:
    rows = await connection_repo.list_blocked_by(db, user.id)
    others = await user_repo.get_many_by_ids(db, {row.addressee_id for row in rows})
    return [
        BlockedUserOut(
            user=UserBrief(
                id=others[row.addressee_id].id,
                display_name=others[row.addressee_id].display_name,
            ),
            blocked_at=row.responded_at or row.created_at,
        )
        for row in rows
        if row.addressee_id in others
    ]
