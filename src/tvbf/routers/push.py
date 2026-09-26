"""Unauthenticated push endpoints (NEU-1484, project spec §5.4).

Only the VAPID public key lives here. The per-user subscription routes are
`/me/push/*` and carry the cookie session and CSRF the rest of `/me` does.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from tvbf.config import Settings, get_settings

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
