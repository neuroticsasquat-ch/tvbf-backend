"""POST /admin/debug/raise-test-error — the `tvbf.debug.raise-test-error` trigger (NEU-1589).

Raises on purpose so the runbook can prove the whole error path end to end:
SDK -> Bugsink -> Discord `#alerts` (NEU-1597). Bearer-token gated like
`admin.py` and `invites_admin.py`, so the filename takes the `*_admin` word
order and `curl` can reach it without a session.

The exception is left unhandled: the Sentry FastAPI integration reports what
escapes a route, so catching it here would test a path production errors never
take.
"""

from fastapi import APIRouter, Depends

from tvbf.deps import require_admin

router = APIRouter(prefix="/admin/debug", tags=["admin"], dependencies=[Depends(require_admin)])


class TelemetryTestError(RuntimeError):
    """Its own type, so Bugsink groups every trigger into one issue that no real
    failure can be mistaken for."""


@router.post("/raise-test-error")
async def raise_test_error() -> None:
    raise TelemetryTestError("tvbf.debug.raise-test-error: deliberate test error (NEU-1589)")
