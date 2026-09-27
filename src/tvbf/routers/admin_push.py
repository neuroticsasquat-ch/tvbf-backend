"""GET /admin/push/stats — push delivery stats for the admin page (NEU-1493, spec §5.4).

Read-only over `app.push_subscription` and `app.push_delivery`: no new table
and no extra column. A retirement is counted off the `failed` delivery row that
recorded it, which outlives the deleted subscription because `subscription_id`
is SET NULL (§4.3).

Cookie-session gated like `admin_users.py` and `admin_reports.py` — the SPA's
admin page reads it — so the filename takes the `admin_*` word order.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.repos import push_delivery_repo, push_subscription_repo
from tvbf.app.schemas import PushStatsDay, PushStatsOut
from tvbf.deps import get_session, require_admin_user

router = APIRouter(
    prefix="/admin/push",
    tags=["admin"],
    dependencies=[Depends(require_admin_user)],
)

STATS_DAYS = 30

# Counts that move every time the delivery job runs or a user toggles a device;
# a heuristically cached body would show an admin yesterday's numbers.
_STATS_CACHE = "private, no-store"


@router.get("/stats", response_model=PushStatsOut)
async def push_stats_route(
    response: Response, db: AsyncSession = Depends(get_session)
) -> PushStatsOut:
    """The subscription totals and `STATS_DAYS` UTC days ending today, oldest
    first. Every day is present — a day with no deliveries is zeroes, so the
    panel charts the series without filling gaps itself."""
    response.headers["Cache-Control"] = _STATS_CACHE
    today = datetime.now(UTC).date()
    first = today - timedelta(days=STATS_DAYS - 1)
    subscriptions, users_subscribed = await push_subscription_repo.count_totals(db)
    counts = await push_delivery_repo.daily_counts_since(db, first)
    by_day = []
    for offset in range(STATS_DAYS):
        day = first + timedelta(days=offset)
        sent, failed, retired = counts.get(day, (0, 0, 0))
        by_day.append(PushStatsDay(day=day, sent=sent, failed=failed, retired=retired))
    return PushStatsOut(
        subscriptions=subscriptions, users_subscribed=users_subscribed, by_day=by_day
    )
