import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.errors import (
    EmailInUse,
    HandleUnavailable,
    InvalidCredentials,
    InvalidInvite,
    TooManyAttempts,
)
from tvbf.app.models import User
from tvbf.app.schemas import (
    AuthedUserOut,
    LoginRequest,
    PasswordChangeRequest,
    SignupRequest,
)
from tvbf.app.services import account_service, auth_throttle
from tvbf.client_ip import client_ip
from tvbf.config import Settings, Throttle, get_settings
from tvbf.cookies import clear_auth_cookies, set_auth_cookies
from tvbf.deps import get_current_user, get_session, require_csrf
from tvbf.integrations import turnstile

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


async def _enforce_ip_throttle(
    db: AsyncSession, *, kind: str, ip: str | None, throttle: Throttle
) -> None:
    """429 when this address has spent its budget. `detail` is the same
    `rate_limited` string `email_change.py` and `email_verification.py` already
    use, and `Retry-After` holds the window."""
    try:
        await auth_throttle.enforce(db, kind=kind, ip=ip, throttle=throttle)
    except TooManyAttempts as err:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited",
            headers={"Retry-After": str(err.retry_after_seconds)},
        ) from err


async def _verify_turnstile(token: str | None, *, ip: str | None, settings: Settings) -> None:
    """No-op when verification is off. Otherwise fail closed: an unverifiable
    token means no account (NEU-1160 §5.1)."""
    if not settings.turnstile_enabled:
        return
    if not token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="captcha_required")
    # `create_app()` refuses to boot enabled-without-a-secret, so this is set.
    secret = settings.turnstile_secret_key or ""
    try:
        await turnstile.verify(token=token, secret=secret, remote_ip=ip)
    except turnstile.TurnstileRejected as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="captcha_invalid"
        ) from err
    except turnstile.TurnstileUnavailable as err:
        log.warning("turnstile verification unavailable", exc_info=err)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="captcha_unavailable"
        ) from err


@router.post("/signup", status_code=status.HTTP_201_CREATED, response_model=AuthedUserOut)
async def signup(
    payload: SignupRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AuthedUserOut:
    ip = client_ip(request, trusted_proxy_hops=settings.trusted_proxy_hops)
    await _enforce_ip_throttle(
        db, kind=auth_throttle.SIGNUP, ip=ip, throttle=settings.signup_ip_throttle
    )
    # Recorded and committed **before** anything can fail, so every attempt that
    # gets this far counts whatever happens after it — a bot spraying invalid
    # tokens burns its budget on rejections, which is the point. Turnstile is
    # verified after the throttle so a flood cannot make us spend an outbound
    # request per attempt.
    await auth_throttle.record(db, kind=auth_throttle.SIGNUP, ip=ip)
    await _verify_turnstile(payload.turnstile_token, ip=ip, settings=settings)
    try:
        user, sess_id, csrf = await account_service.signup(
            db,
            email=str(payload.email),
            password=payload.password,
            display_name=payload.display_name,
            handle=payload.handle,
            invite_code=payload.invite_code,
            ttl_days=settings.session_ttl_days,
            user_agent=request.headers.get("user-agent"),
            ip=ip,
            frontend_base_url=settings.frontend_base_url,
        )
    except InvalidInvite as err:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid_invite") from err
    except EmailInUse as err:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email_in_use") from err
    except HandleUnavailable as err:
        # `409`, matching `email_in_use` — the same class of thing on the same
        # form, so the SPA maps both conflicts to their field in one place
        # rather than learning two shapes (NEU-1163 §6.3).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="handle_unavailable"
        ) from err
    set_auth_cookies(response, session_id=sess_id, csrf=csrf, settings=settings)
    return AuthedUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        handle=user.handle,
        created_at=user.created_at,
        email_verified_at=user.email_verified_at,
        csrf_token=csrf,
        activity_feed_enabled=user.activity_feed_enabled,
        is_admin=user.is_admin,
    )


@router.post("/login", response_model=AuthedUserOut)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AuthedUserOut:
    ip = client_ip(request, trusted_proxy_hops=settings.trusted_proxy_hops)
    await _enforce_ip_throttle(
        db, kind=auth_throttle.LOGIN, ip=ip, throttle=settings.login_ip_throttle
    )
    try:
        user, sess_id, csrf = await account_service.authenticate(
            db,
            email=str(payload.email),
            password=payload.password,
            ttl_days=settings.session_ttl_days,
            user_agent=request.headers.get("user-agent"),
            ip=ip,
            lockout_threshold=settings.login_lockout_threshold,
            lockout_window_minutes=settings.login_lockout_window_minutes,
        )
    except InvalidCredentials as err:
        # Only failures are counted. Credential stuffing is made of failures, so
        # the signal is intact, while a shared office address whose occupants all
        # log in successfully never accumulates a count.
        await auth_throttle.record(db, kind=auth_throttle.LOGIN, ip=ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials"
        ) from err
    set_auth_cookies(response, session_id=sess_id, csrf=csrf, settings=settings)
    return AuthedUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        handle=user.handle,
        created_at=user.created_at,
        email_verified_at=user.email_verified_at,
        csrf_token=csrf,
        activity_feed_enabled=user.activity_feed_enabled,
        is_admin=user.is_admin,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    sess_id = request.cookies.get(settings.session_cookie_name)
    if sess_id:
        await account_service.logout(db, session_id=sess_id)
    clear_auth_cookies(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post(
    "/password",
    response_model=AuthedUserOut,
    dependencies=[Depends(require_csrf)],
)
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AuthedUserOut:
    try:
        sess_id, csrf = await account_service.change_password(
            db,
            user=user,
            current_password=payload.current_password,
            new_password=payload.new_password,
            ttl_days=settings.session_ttl_days,
            user_agent=request.headers.get("user-agent"),
            ip=client_ip(request, trusted_proxy_hops=settings.trusted_proxy_hops),
        )
    except InvalidCredentials as err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials"
        ) from err
    set_auth_cookies(response, session_id=sess_id, csrf=csrf, settings=settings)
    return AuthedUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        handle=user.handle,
        created_at=user.created_at,
        email_verified_at=user.email_verified_at,
        csrf_token=csrf,
        activity_feed_enabled=user.activity_feed_enabled,
        is_admin=user.is_admin,
    )
