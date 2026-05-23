"""POST /me/feedback — forward a user's feedback to Linear as an issue +
attached CustomerNeed. Returns 204 on success, 502 on Linear-side failure,
503 if the feature flag is off, 422 on invalid input."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from tvbf.app.models import User
from tvbf.app.services import feedback_service
from tvbf.config import Settings, get_settings
from tvbf.deps import get_current_user, get_linear_client, require_csrf
from tvbf.integrations.linear import LinearClient, LinearError

log = logging.getLogger(__name__)

router = APIRouter(tags=["feedback"])


class FeedbackIn(BaseModel):
    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=5000)


@router.post(
    "/me/feedback",
    status_code=204,
    dependencies=[Depends(require_csrf)],
)
async def submit_feedback_route(
    payload: FeedbackIn,
    user: User = Depends(get_current_user),
    linear: LinearClient = Depends(get_linear_client),
    settings: Settings = Depends(get_settings),
) -> Response:
    if not settings.linear_feedback_enabled:
        raise HTTPException(status_code=503, detail="Feedback is currently disabled.")
    try:
        await feedback_service.submit_feedback(
            user=user,
            subject=payload.subject,
            body=payload.body,
            linear=linear,
            settings=settings,
        )
    except LinearError as exc:
        log.exception("linear feedback submission failed user_id=%s", user.id)
        raise HTTPException(status_code=502, detail="Could not submit feedback.") from exc
    return Response(status_code=204)
