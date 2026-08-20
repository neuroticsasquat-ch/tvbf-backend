"""POST /reports — a user reports another user (NEU-1162 §7.2).

Always **204** once the row is committed, whatever the notification path does
afterwards: the reporter is told "received" exactly when we have genuinely
received it. `report_service` carries why that differs from `/me/feedback`.

Two gates deliberately absent. **No `require_verified_user`** — NEU-1161's rule
is that a verified mailbox is the price of *outreach*, of touching another user,
and a report touches Tom rather than the reported user. Gating it would invert
the protection in the worst case: the account most likely to be unverified is a
new one, which is exactly who a griefer targets. **No `get_linear_client`
dependency** — that dep raises 503 when no client is configured, which would
undo the commit-then-notify contract; the client is read off app state and
`None` means "skip the issue, still send the email".
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import NotFound, SelfReportForbidden, TooManyAttempts
from tvbf.app.models import User
from tvbf.app.services import report_service
from tvbf.config import Settings, get_settings
from tvbf.deps import get_current_user, get_session, require_csrf
from tvbf.integrations.linear import LinearClient

router = APIRouter(tags=["reports"])


class ReportIn(BaseModel):
    """Beside its route rather than in `app/schemas.py`, mirroring `FeedbackIn`
    — the shape this one is deliberately matched to. NEU-1162's *other* new
    body, `AdminUserDisabledUpdateRequest`, sits in `schemas.py` because
    `AdminUserUpdateRequest` does. Each follows the neighbour it mirrors, which
    is why the two land in different files."""

    reported_user_id: UUID
    # 1–5000, matching `FeedbackIn.body` — the same kind of free text arriving
    # from the same SPA.
    reason: str = Field(min_length=1, max_length=5000)


@router.post(
    "/reports",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def submit_report_route(
    payload: ReportIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    linear: LinearClient | None = getattr(request.app.state, "linear_client", None)
    try:
        await report_service.submit_report(
            db,
            reporter=user,
            reported_user_id=payload.reported_user_id,
            reason=payload.reason,
            linear=linear,
            settings=settings,
        )
    except SelfReportForbidden as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="cannot_report_self"
        ) from err
    except NotFound as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="reported_user_not_found"
        ) from err
    except TooManyAttempts as err:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited",
            headers={"Retry-After": str(err.retry_after_seconds)},
        ) from err
    return Response(status_code=status.HTTP_204_NO_CONTENT)
