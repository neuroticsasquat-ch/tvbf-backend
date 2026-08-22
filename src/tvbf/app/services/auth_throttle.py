"""The IP-keyed throttle on `/auth/signup` and `/auth/login` (NEU-1160).

Complementary to the email-keyed lockout in `account_service.authenticate`,
never a replacement for it: that one answers "has this *account* been guessed
at?", this one answers "how much has this *address* already done?" — the
question credential stuffing that walks a different email every request makes
invisible to the first.

Two rules are load-bearing and easy to undo:

* **A request rejected with 429 is not itself recorded.** The window drains and
  a throttled address recovers without intervention; recording rejections would
  let a script hold itself banned indefinitely, which sounds appealing until the
  script belongs to a NAT'd office.
* **A successful login never clears the address's counter**, unlike the
  email-keyed one. An attacker owns at least one valid account — their own — so
  clear-on-success would hand them a reset button between every ten guesses.

`ip=None` means the request is **not** throttled: `client_ip` could not resolve
a trustworthy address, and there is no key to count against (see that module for
why that is the right failure).
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import TooManyAttempts
from tvbf.app.repos import auth_attempt_repo
from tvbf.app.repos.auth_attempt_repo import CONTACT as CONTACT
from tvbf.app.repos.auth_attempt_repo import LOGIN as LOGIN
from tvbf.app.repos.auth_attempt_repo import SIGNUP as SIGNUP
from tvbf.config import Throttle


async def enforce(db: AsyncSession, *, kind: str, ip: str | None, throttle: Throttle) -> None:
    """Raise `TooManyAttempts` when `ip` is at or above the budget for `kind`.

    **The count and the insert are not locked together**, unlike
    `rate_budget.DatabaseRateLimiter`'s `SELECT ... FOR UPDATE`: requests racing
    on one address can all read the same count and all pass. Accepted rather
    than overlooked — the budgets here are 5/hour and 10/15min against a bucket
    that is 20/second, the overshoot is bounded by concurrency rather than
    unbounded, and a row lock on every signup and failed login would put a
    serialisation point on the two routes an attacker is already flooding.
    """
    if ip is None:
        return
    since = datetime.now(UTC) - timedelta(minutes=throttle.window_minutes)
    attempts = await auth_attempt_repo.count_since(db, kind=kind, ip=ip, since=since)
    if attempts >= throttle.max_attempts:
        # The window in seconds, not a computed remainder: the exact answer needs
        # the oldest row in the window, and a second query to shave seconds off a
        # rejection buys nothing. An honest upper bound is the right shape here.
        raise TooManyAttempts(retry_after_seconds=throttle.window_minutes * 60)


async def record(db: AsyncSession, *, kind: str, ip: str | None) -> None:
    """Record one attempt and commit it.

    The commit is load-bearing rather than incidental: `account_service.signup`
    calls `db.rollback()` on its `IntegrityError` path (`EmailInUse`), which
    would otherwise discard the attempt row and make a duplicate-email spray
    free.
    """
    if ip is None:
        return
    await auth_attempt_repo.record(db, kind=kind, ip=ip)
    await db.commit()
