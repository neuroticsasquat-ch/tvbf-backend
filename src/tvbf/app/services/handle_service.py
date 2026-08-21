"""Claiming and changing a handle (NEU-1163 §6).

One module for the rule both write sites need — *may this account have this
handle?* — plus the change itself and its budget. `POST /signup` reaches
`ensure_claimable` through `account_service.signup`; `PATCH /me/handle` reaches
both functions here.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import HandleUnavailable, TooManyAttempts
from tvbf.app.models import User
from tvbf.app.repos import handle_release_repo, user_repo
from tvbf.config import Settings

# `IntegrityError.orig` carries the constraint name, and this is the one that
# says *handle*. `app.user` grew a second unique constraint with this ticket, so
# a blanket `except IntegrityError` can no longer name a field (§6.4).
HANDLE_CONSTRAINT = "uq_user_handle"


async def ensure_claimable(
    db: AsyncSession, *, handle: str, claimant_id: UUID | None = None
) -> None:
    """Raise `HandleUnavailable` unless `claimant_id` may take `handle`.

    Two refusals, **one indistinguishable answer** (§6.3): a live account holds
    it, or a *different* account released it. Distinguishing them is more
    helpful — the second is permanent — and it turns this into a *has this
    handle ever existed* oracle, including for accounts since deleted.

    A handle its own former owner released is claimable by them and by nobody
    else. That same-owner exemption is what keeps "never reusable" from being a
    trap; `claimant_id` is `None` at signup, where by construction there is no
    former self to exempt.

    **Not locked.** The answer can change between here and the commit, which is
    why `account_service.signup` and `change_handle` both keep an
    `IntegrityError` fallback: this check produces the right refusal without a
    rollback in every case except a genuine race, and the race still gets a
    correct answer.
    """
    holder = await user_repo.get_by_handle(db, handle)
    if holder is not None and holder.id != claimant_id:
        raise HandleUnavailable()
    released = await handle_release_repo.get(db, handle)
    if released is not None and released.user_id != claimant_id:
        raise HandleUnavailable()


def is_handle_conflict(err: IntegrityError) -> bool:
    """Whether `err` is `uq_user_handle` rather than `uq_user_email`."""
    return HANDLE_CONSTRAINT in str(getattr(err, "orig", err))


async def _enforce_change_throttle(db: AsyncSession, *, user_id: UUID, settings: Settings) -> None:
    """Raise `TooManyAttempts` at or above the change budget (§6.2).

    The ledger is `app.handle_release` itself — NEU-1162's shape, where the row
    is written anyway so there is no side table and no "record the attempt" step
    to forget.

    The un-locked count-then-insert race is inherited from
    `auth_throttle.enforce` and accepted for the reason stated there.
    """
    throttle = settings.handle_change_throttle
    since = datetime.now(UTC) - timedelta(minutes=throttle.window_minutes)
    released = await handle_release_repo.count_since(db, user_id=user_id, since=since)
    if released >= throttle.max_attempts:
        raise TooManyAttempts(retry_after_seconds=throttle.window_minutes * 60)


async def change_handle(db: AsyncSession, *, user: User, handle: str, settings: Settings) -> None:
    """Move `user` onto `handle`, recording the release of the old one.

    **The release row and the update commit together.** A commit that moved the
    handle without recording the release would free it silently, which is the
    one way §4.2's never-reusable rule can be broken from inside.

    Changing to the handle you already hold is a **no-op**, not a change: it
    spends no budget and writes no release row. Refusing it would be a 409
    against yourself, and letting it through unguarded would let a user burn
    their month's allowance on a form they never edited.
    """
    if user.handle == handle:
        return

    await _enforce_change_throttle(db, user_id=user.id, settings=settings)
    await ensure_claimable(db, handle=handle, claimant_id=user.id)

    old_handle = user.handle
    user.handle = handle
    await handle_release_repo.record(db, handle=old_handle, user_id=user.id)
    try:
        await db.commit()
    except IntegrityError as err:
        await db.rollback()
        if is_handle_conflict(err):
            raise HandleUnavailable() from err
        raise
    await db.refresh(user)


async def release_on_delete(db: AsyncSession, *, user: User) -> None:
    """Record the handle of an account about to be deleted (§4.2).

    Inserted **before** the user row goes, so the FK is satisfiable; the
    `ON DELETE SET NULL` then nulls the owner as the delete cascades. A null
    owner means the handle is blocked and reclaimable by nobody, which is the
    correct end state for an identity its owner disclaimed — and it is what
    stops self-service account deletion being a supported route to handing your
    identity to a stranger.
    """
    await handle_release_repo.record(db, handle=user.handle, user_id=user.id)
