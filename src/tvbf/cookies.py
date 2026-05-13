"""Shared helpers for setting/clearing the session+CSRF cookie pair.

Lifted out of `routers/auth.py` so other routers (e.g. session revocation)
can clear cookies without reaching into auth's private helpers.
"""

from __future__ import annotations

from fastapi import Response

from tvbf.config import Settings


def set_auth_cookies(
    response: Response,
    *,
    session_id: str,
    csrf: str,
    settings: Settings,
) -> None:
    max_age = settings.session_ttl_days * 86400
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_id,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        path="/",
        domain=settings.cookie_domain,
    )
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf,
        max_age=max_age,
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        path="/",
        domain=settings.cookie_domain,
    )


def clear_auth_cookies(response: Response, settings: Settings) -> None:
    for name in (settings.session_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(
            key=name,
            path="/",
            secure=settings.cookie_secure,
            samesite=settings.cookie_samesite,  # type: ignore[arg-type]
            httponly=name == settings.session_cookie_name,
            domain=settings.cookie_domain,
        )
