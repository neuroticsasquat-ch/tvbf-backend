"""Push endpoints (project spec §5.4).

`GET /push/vapid-public-key` is unauthenticated (NEU-1484). The per-user
subscription routes under `/me/push/*` (NEU-1485) and the test send (NEU-1486)
carry the cookie session, and the mutating ones CSRF, exactly as the rest of
`/me` does.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import NotFound, TooManyAttempts
from tvbf.app.models import User
from tvbf.app.repos import push_subscription_repo
from tvbf.app.schemas import (
    PushSubscriptionCreated,
    PushSubscriptionIn,
    PushSubscriptionOut,
    PushTestIn,
    PushTestOut,
)
from tvbf.app.services import push_test_service
from tvbf.config import Settings, get_settings
from tvbf.deps import get_current_user, get_session, require_csrf

router = APIRouter(tags=["push"])


class VapidPublicKeyOut(BaseModel):
    public_key: str


@router.get("/push/vapid-public-key", response_model=VapidPublicKeyOut)
async def vapid_public_key(
    response: Response, settings: Settings = Depends(get_settings)
) -> VapidPublicKeyOut:
    """The key the SPA passes to `PushManager.subscribe` as `applicationServerKey`.

    **Unauthenticated and `public`**, unlike browse: it is the same public key
    for every caller and carries nothing about any user, so a shared cache may
    hold it. A day is how long a rotation takes to reach a browser that already
    fetched it, which the 404/410 retirement absorbs anyway (§7).

    503 when any of the three VAPID values is unset — not just the public one,
    because a key the server cannot sign pushes for would let the SPA create
    subscriptions nothing will ever deliver to.
    """
    if not settings.vapid_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="vapid_not_configured"
        )
    response.headers["Cache-Control"] = "public, max-age=86400"
    return VapidPublicKeyOut(public_key=settings.vapid_public_key or "")


@router.get("/me/push/subscriptions", response_model=list[PushSubscriptionOut])
async def list_my_push_subscriptions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> list[PushSubscriptionOut]:
    """The caller's devices, newest first — never an endpoint or a key."""
    rows = await push_subscription_repo.list_for_user(db, user.id)
    return [
        PushSubscriptionOut(
            id=row.id,
            user_agent=row.user_agent,
            created_at=row.created_at,
            last_success_at=row.last_success_at,
        )
        for row in rows
    ]


@router.post(
    "/me/push/subscriptions",
    response_model=PushSubscriptionCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_csrf)],
)
async def create_my_push_subscription(
    payload: PushSubscriptionIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> PushSubscriptionCreated:
    """Store this browser's subscription, upserting on the endpoint (§4.2).

    An endpoint already held by anyone — this user or another — moves onto the
    caller, so `201` whether or not a row existed. 503 when VAPID is not
    configured, for the public-key route's reason: a subscription nothing can
    ever deliver to is worse than a clear refusal.
    """
    if not settings.vapid_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="vapid_not_configured"
        )
    subscription_id = await push_subscription_repo.upsert(
        db,
        user_id=user.id,
        endpoint=payload.endpoint,
        p256dh=payload.keys.p256dh,
        auth=payload.keys.auth,
        user_agent=request.headers.get("user-agent") or None,
    )
    await db.commit()
    return PushSubscriptionCreated(id=subscription_id)


@router.delete(
    "/me/push/subscriptions/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def delete_my_push_subscription(
    subscription_id: Annotated[UUID, Path()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Forget one device. 404 for an id that is not the caller's, whether or
    not it exists, so the route does not confirm other users' ids."""
    deleted = await push_subscription_repo.delete_for_user(
        db, user_id=user.id, subscription_id=subscription_id
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/me/push/subscriptions",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def delete_all_my_push_subscriptions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """The "turn everything off" switch: every one of the caller's devices.
    204 even when there were none."""
    await push_subscription_repo.delete_all_for_user(db, user.id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/me/push/test",
    response_model=PushTestOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_csrf)],
)
async def send_my_test_push(
    payload: PushTestIn,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> PushTestOut:
    """Push the §5.3 `test` notification to one of the caller's devices, now.

    202 with the outcome in the body — `sent` or `failed`, and the push
    service's status — rather than a bare 202 that would hide a failure from
    the one person watching for it. **410 when the push service says the
    subscription is gone**: the row is deleted and the client should drop its
    local subscription too. 404 `not_found` for an id that is not the caller's;
    429 `rate_limited` past `PUSH_TEST_THROTTLE_MAX` per window; 503
    `vapid_not_configured` first, since nothing can be sent without it.
    """
    if not settings.vapid_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="vapid_not_configured"
        )
    try:
        outcome = await push_test_service.send_test_push(
            db, user_id=user.id, subscription_id=payload.subscription_id, settings=settings
        )
    except TooManyAttempts as err:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited",
            headers={"Retry-After": str(err.retry_after_seconds)},
        ) from err
    except NotFound as err:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found") from err
    if outcome.status == "gone":
        response.status_code = status.HTTP_410_GONE
    return PushTestOut(status=outcome.status, status_code=outcome.status_code)
