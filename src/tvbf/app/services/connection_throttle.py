"""The per-requester budget on `POST /connection-requests` (NEU-1157).

Sibling of `auth_throttle`, and keyed on a *user* rather than an address: this
governs outreach, not authentication. It owns two refusals.

**The daily cap** counts requests the caller has *created* in a rolling window.
All five outcomes count, so cancel-and-re-send is worthless as a reset.

**The ceiling that cap is measured against is not a constant.** It is one of two
`Throttle`s, selected by the caller's recent reputation — `MAX` normally,
dropping to `FLOOR` when more of their resolved requests are being rejected or
ignored than accepted. Demotion rather than earn-your-ceiling is a deliberate
product choice recorded in the spec: starting everyone at the floor is strictly
the stronger posture, and the only one that bounds a day-one spammer before any
signal accrues, but it taxes the honest new user hardest at exactly the moment
the app needs them to build a friend graph. **The consequence is that a fresh
account runs its first day at the full ceiling**, which is what makes the choice
of `MAX` carry the security weight rather than this rule.

A separate module rather than inlined `report_service`-style because, unlike the
report budget, the ceiling derivation is non-trivial and worth testing on its
own; and not folded into `connection_service`, already the largest service here.

**The ledger writes live in `connection_service`**, beside the lifecycle
transitions they mirror. Putting them here would mean `block()` importing a
throttle in order to record a block, inverting the dependency.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import DeclineCooldownActive, TooManyAttempts
from tvbf.app.repos import connection_request_log_repo
from tvbf.config import Settings, Throttle


async def current_ceiling(db: AsyncSession, *, requester_id: UUID, settings: Settings) -> Throttle:
    """Which of the two budgets applies to `requester_id` right now (§3.2).

    The floor applies when the sample is large enough **and** the adverse share
    reaches the threshold. Evaluated in **integer arithmetic** rather than a
    float rate, so there is no rounding behaviour to argue about at the boundary
    and a test can assert it exactly.

    Three exclusions carry reasons:

    * `cancelled` is in neither numerator nor denominator. The cap already makes
      cancel-and-re-send worthless — the slot never comes back — so counting a
      cancel as a rejection charges twice for one act, and the person it reaches
      is the honest user fixing a mistake. A spammer has no reason to cancel at
      all. The hole this leaves is chosen knowingly: an account can send its
      daily ten, cancel all ten, and its rate is unmoved. It is acceptable
      because *the daily cap is the thing bounding harm*; this rule exists only
      to lower that cap for accounts whose recipients visibly reject them.
    * **Young `pending` rows are in neither bucket.** In the denominator they
      would dominate every account's rate with requests nobody has had a chance
      to answer, and an honest burst would self-demote within minutes.
    * It is a **rate, not an absolute count**. `MAX - adverse` needs no minimum
      sample and is simpler, but it demotes a popular user who sent 30 requests
      and had 25 accepted — to a subtraction rule, volume and rejection are the
      same axis, which is precisely the distinction this is meant to draw.

    A rolling window means **automatic recovery**: a demoted account returns to
    the full ceiling once the bad rows age out, without changing its behaviour.
    That is right here — the remedy for a persistent abuser is report → disable
    (NEU-1162). This is a speed limit, not a sentence.
    """
    rule = settings.connection_request_reputation
    now = datetime.now(UTC)
    accepted, adverse = await connection_request_log_repo.reputation_counts(
        db,
        requester_id=requester_id,
        since=now - timedelta(days=rule.window_days),
        ignored_before=now - timedelta(days=rule.ignored_after_days),
    )
    sample = accepted + adverse
    if sample >= rule.min_sample and adverse * 100 >= sample * rule.adverse_percent:
        return settings.connection_request_floor_throttle
    return settings.connection_request_throttle


async def enforce(db: AsyncSession, *, requester_id: UUID, settings: Settings) -> None:
    """Raise `TooManyAttempts` when `requester_id` is at or above their ceiling.

    **The count and the insert are not locked together**, inherited from
    `auth_throttle.enforce` and accepted for the reason stated there: concurrent
    requests can read the same count and all pass, the overshoot is bounded by
    concurrency, and a row lock on every create would put a serialisation point
    on the route an attacker is already flooding.

    A **mid-day demotion binds immediately** — nothing already sent is
    retracted, but if the ceiling drops below what the account has already sent
    today, the next request is refused. `count >= ceiling`, matching both
    existing throttles.
    """
    throttle = await current_ceiling(db, requester_id=requester_id, settings=settings)
    since = datetime.now(UTC) - timedelta(minutes=throttle.window_minutes)
    created = await connection_request_log_repo.count_created_since(
        db, requester_id=requester_id, since=since
    )
    if created >= throttle.max_attempts:
        # The whole window in seconds, not a computed remainder: the exact
        # answer needs the oldest row in the window, and a second query to shave
        # seconds off a rejection buys nothing. `auth_throttle.enforce` and
        # `report_service.submit_report` both already reason this way.
        raise TooManyAttempts(retry_after_seconds=throttle.window_minutes * 60)


async def enforce_decline_cooldown(
    db: AsyncSession, *, requester_id: UUID, addressee_id: UUID, settings: Settings
) -> None:
    """Raise `DeclineCooldownActive` when the addressee declined this requester
    within the cooldown (§4).

    This closes the *targeted* case, which the daily cap does not reach and
    which the existing 409 does not either: `delete_pending_request` **deletes**
    the `connection` row, so `find_pair` returns `None` afterwards and the same
    request succeeds. Without this, a requester may send their whole daily
    allowance to one person who has explicitly said no, every day, indefinitely.

    Bounded rather than permanent: `block` already exists as the explicit
    permanent tool and it belongs to the recipient, so making a decline
    permanent would quietly convert a soft "no" into a hard one the decliner
    never chose. The accepted cost is that an accidental decline — and the
    decline control sits next to the accept control — cannot be corrected for
    the length of the cooldown, and neither party is told why.
    """
    since = datetime.now(UTC) - timedelta(days=settings.connection_request_decline_cooldown_days)
    if await connection_request_log_repo.has_decline_since(
        db, requester_id=requester_id, addressee_id=addressee_id, since=since
    ):
        raise DeclineCooldownActive()
