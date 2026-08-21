from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import EmailInUse, HandleUnavailable, InvalidCredentials, InvalidInvite
from tvbf.app.models import User
from tvbf.app.passwords import hash_password, verify_password
from tvbf.app.repos import invite_repo, login_attempt_repo, session_repo, user_repo
from tvbf.app.services import handle_service
from tvbf.app.tokens import new_csrf_token, new_session_id


async def signup(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str,
    handle: str,
    invite_code: str,
    ttl_days: int,
    user_agent: str | None,
    ip: str | None,
    frontend_base_url: str | None = None,
) -> tuple[User, str, str]:
    """Create a new user, open a session, and return (user, session_id, csrf_token).
    Requires a valid unconsumed invite code; raises InvalidInvite otherwise.
    Raises EmailInUse on duplicate email, HandleUnavailable on a taken handle."""
    invite = await invite_repo.get(db, invite_code)
    if invite is None or invite.consumed_at is not None:
        # Don't differentiate between "unknown" and "consumed" — keeps the
        # signup endpoint from leaking which codes were ever issued.
        raise InvalidInvite()
    if invite.email_hint is not None and invite.email_hint.lower() != email.lower():
        raise InvalidInvite()

    # The pre-check runs first because it is the only thing that can distinguish
    # "held by a live account" from "released by a different one", and because
    # it produces the refusal without a rollback. It is not locked, so the
    # `IntegrityError` branch below stays as the race fallback.
    await handle_service.ensure_claimable(db, handle=handle)

    password_hash = hash_password(password)
    try:
        user = await user_repo.create(
            db,
            email=email,
            password_hash=password_hash,
            display_name=display_name,
            handle=handle,
        )
    except IntegrityError as err:
        # **Dispatched on the constraint name** (NEU-1163 §6.4). This was a
        # blanket `raise EmailInUse()` while `app.user` carried exactly one
        # unique constraint; with `uq_user_handle` beside `uq_user_email`, a
        # duplicate handle would report `email_in_use` — a refusal naming the
        # wrong field, on the one form where both are submitted together.
        await db.rollback()
        if handle_service.is_handle_conflict(err):
            raise HandleUnavailable() from err
        raise EmailInUse() from err

    await invite_repo.consume(db, invite=invite, user_id=user.id, consumed_at=datetime.now(UTC))

    sess_id = new_session_id()
    csrf = new_csrf_token()
    await session_repo.create(
        db,
        session_id=sess_id,
        user_id=user.id,
        ttl_days=ttl_days,
        user_agent=user_agent,
        ip=ip,
    )
    await db.commit()
    await db.refresh(user)

    if frontend_base_url is not None:
        # Best-effort send: a failure here must not 500 the signup.
        from tvbf.app.services import email_verification_service

        await email_verification_service.send_verification_email_best_effort(
            db, user=user, frontend_base_url=frontend_base_url
        )

    return user, sess_id, csrf


async def authenticate(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    ttl_days: int,
    user_agent: str | None,
    ip: str | None,
    lockout_threshold: int = 5,
    lockout_window_minutes: int = 15,
) -> tuple[User, str, str]:
    """Verify credentials, open a new session, return (user, session_id, csrf_token).
    Raises InvalidCredentials on bad email/password OR when the email has hit
    the brute-force threshold (we deliberately return the same error so attackers
    can't tell whether they're locked out)."""
    since = datetime.now(UTC) - timedelta(minutes=lockout_window_minutes)
    failures = await login_attempt_repo.count_since(db, email=email, since=since)
    if failures >= lockout_threshold:
        raise InvalidCredentials()

    user = await user_repo.get_by_email(db, email)
    if user is None or not verify_password(password, user.password_hash):
        await login_attempt_repo.record(db, email=email, ip=ip)
        await db.commit()
        raise InvalidCredentials()

    # A disabled account is refused with the *same* generic error a wrong
    # password gets (NEU-1162 §2.3) — an abuser who has just been disabled is
    # not told they were caught. Three properties hold this up, and all three
    # come from where the check sits:
    #
    # * **After `verify_password`, deliberately.** Argon2 is slow on purpose;
    #   answering a disabled account before it would take milliseconds where
    #   every other 401 takes ~100ms, which is a timing oracle saying exactly
    #   what the generic error refuses to say.
    # * **No `app.login_attempt` row.** That ledger answers "is this *account*
    #   being guessed at?" and the guess was correct — recording it would poison
    #   the brute-force signal and eventually lock the account out for an
    #   unrelated reason.
    # * **Before `clear_for_email`,** so a disabled account cannot be used as a
    #   reset button on the email-keyed lockout.
    #
    # The IP throttle still records an attempt, with no code here: the router's
    # existing `except InvalidCredentials` branch does it.
    if user.disabled_at is not None:
        raise InvalidCredentials()

    # Successful login — wipe the slate clean.
    await login_attempt_repo.clear_for_email(db, email=email)

    sess_id = new_session_id()
    csrf = new_csrf_token()
    await session_repo.create(
        db,
        session_id=sess_id,
        user_id=user.id,
        ttl_days=ttl_days,
        user_agent=user_agent,
        ip=ip,
    )
    await db.commit()
    return user, sess_id, csrf


async def logout(db: AsyncSession, *, session_id: str) -> None:
    """Delete the session row. No-op if session doesn't exist."""
    await session_repo.delete(db, session_id)
    await db.commit()


async def change_password(
    db: AsyncSession,
    *,
    user: User,
    current_password: str,
    new_password: str,
    ttl_days: int,
    user_agent: str | None,
    ip: str | None,
) -> tuple[str, str]:
    """Verify current password, rotate to new password, invalidate all existing
    sessions, create a new one. Returns (session_id, csrf_token).
    Raises InvalidCredentials if current_password is wrong."""
    if not verify_password(current_password, user.password_hash):
        raise InvalidCredentials()

    await user_repo.update_password_hash(db, user, hash_password(new_password))
    await session_repo.delete_all_for_user(db, user.id)

    sess_id = new_session_id()
    csrf = new_csrf_token()
    await session_repo.create(
        db,
        session_id=sess_id,
        user_id=user.id,
        ttl_days=ttl_days,
        user_agent=user_agent,
        ip=ip,
    )
    await db.commit()
    return sess_id, csrf


async def delete_account(db: AsyncSession, *, user: User, password: str) -> None:
    """Verify password then delete user (cascade handles sessions and watch data).
    Raises InvalidCredentials if password is wrong.

    The handle is released **into the same transaction** as the delete
    (NEU-1163 §4.2): inserted while the user row still exists so the FK is
    satisfiable, then orphaned by `ON DELETE SET NULL` as the delete cascades.
    Without it, deleting an account would drop its release rows *and* free its
    live handle, making self-service deletion a supported route to handing your
    identity to a stranger."""
    if not verify_password(password, user.password_hash):
        raise InvalidCredentials()

    await handle_service.release_on_delete(db, user=user)
    await user_repo.delete_user(db, user.id)
    await db.commit()


async def resolve_session_user(db: AsyncSession, *, session_id: str | None) -> User | None:
    """Given a cookie value, return the User if the session is valid.
    Touches the session and commits. Returns None if invalid."""
    if not session_id:
        return None

    sess = await session_repo.get_active(db, session_id)
    if sess is None:
        return None

    user = await user_repo.get_by_id(db, sess.user_id)
    if user is None:  # pragma: no cover  -- defensive: FK cascade prevents this
        return None

    # A disabled account's session is not a valid session (NEU-1162 §2.1). This
    # function has exactly one caller — `deps.get_current_user` — so one
    # predicate here covers browse, `/me`, connections, friend engagement, admin
    # and everything added later, at once, rather than route by route. The
    # caller's existing `401 auth_required` is what the request sees: no new
    # status code and no `account_disabled` detail, because a machine-readable
    # confirmation on every request tells the abuser precisely what happened,
    # and they get one per retry (§2.2).
    if user.disabled_at is not None:
        return None

    await session_repo.touch(db, session_id)
    await db.commit()
    return user
