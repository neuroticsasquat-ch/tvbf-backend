"""GET /admin/reports — the admin report queue (NEU-1197).

`POST /reports` commits the row and *then* notifies best-effort (NEU-1162
§8.1), so the notification is the only way a report becomes visible and it is
the part allowed to fail. This route makes the rows readable over HTTP. It adds
no workflow, no state and no migration: one `SELECT` behind the admin session
gate.

It is a **queue reader, not triage.** NEU-1162 §5 refused a `handled_at` column
deliberately — Linear is the workflow, with states, assignment and comments, and
a coarse copy of that in Postgres gives two records that immediately disagree.
Nothing here writes. The one piece of workflow state it shows, whether the
reported account is currently disabled, is a live join to `app.user.disabled_at`.

Cookie-session gated like `admin_users.py`, not bearer-token gated like
`admin.py`: this is a surface an admin reads in the SPA, not a scripting
endpoint. The filename encodes that, following `admin_invites.py` (cookie) vs.
`invites_admin.py` (bearer) — the word order is the distinction. Deliberately
not folded into `routers/reports.py`, which is the *user's* filing surface with
a per-route `get_current_user`; merging would put two different gates in one
file for the first time in this codebase.
"""

import math
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import User, UserReport
from tvbf.app.repos import user_report_repo
from tvbf.app.schemas import AdminReportOut, AdminReportPage, AdminReportUserRef
from tvbf.deps import get_session, require_admin_user

router = APIRouter(
    prefix="/admin/reports",
    tags=["admin"],
    dependencies=[Depends(require_admin_user)],
)

# The payload carries `disabled_at` for both parties, mutated by
# `PATCH /admin/users/{user_id}/disabled` — same SPA, same admin, often seconds
# apart. Disable someone in the Users tab, open the queue, and a
# heuristically-cached body shows `disabled_at: null` beside their name: the
# queue answering "has this been dealt with?" with the wrong answer. That is a
# wrong decision, not a staleness nuisance. `GET /admin/users` has the identical
# exposure and is deliberately left alone here — a known gap, closed by the
# ticket that owns that route.
_REPORTS_CACHE = "private, no-store"


def _ref(u: User) -> AdminReportUserRef:
    return AdminReportUserRef(
        id=u.id, display_name=u.display_name, handle=u.handle, disabled_at=u.disabled_at
    )


def _out(report: UserReport, reporter: User, reported: User) -> AdminReportOut:
    return AdminReportOut(
        id=report.id,
        reporter=_ref(reporter),
        reported_user=_ref(reported),
        reason=report.reason,
        created_at=report.created_at,
    )


@router.get("", response_model=AdminReportPage)
async def list_reports_route(
    response: Response,
    page: int = Query(default=1, ge=1, le=1000),
    per_page: int = Query(default=50, ge=1, le=100),
    reported_user_id: UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> AdminReportPage:
    """Newest first, paginated on `GET /shows`'s bounds verbatim, so the API
    carries one set of pagination bounds rather than two.

    An unknown `reported_user_id` is an empty page, not a 404: a filter is a
    filter, not a lookup, and `total: 0` *is* the true answer to "has this
    account been reported?" — the question the parameter exists to ask.
    """
    response.headers["Cache-Control"] = _REPORTS_CACHE
    rows, total = await user_report_repo.list_for_admin(
        db, reported_user_id=reported_user_id, page=page, per_page=per_page
    )
    return AdminReportPage(
        items=[_out(report, reporter, reported) for report, reporter, reported in rows],
        page=page,
        per_page=per_page,
        total=total,
        total_pages=max(1, math.ceil(total / per_page)),
    )
