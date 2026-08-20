from __future__ import annotations


class DomainError(Exception):
    """Base for app-level expected errors."""


class EmailInUse(DomainError):
    pass


class InvalidCredentials(DomainError):
    pass


class NotFound(DomainError):
    pass


class InvalidInvite(DomainError):
    """Invite code is unknown, already consumed, or doesn't match the signup email."""

    pass


class SelfConnectionForbidden(DomainError):
    """A user attempted to connect to or block themselves."""


class ConnectionAlreadyExists(DomainError):
    """An existing pair (any non-blocked state) prevents a new request."""

    def __init__(self, existing) -> None:
        super().__init__()
        self.existing = existing


class ConnectionBlocked(DomainError):
    """One side of the pair has blocked the other."""


class NotAConnectionParty(DomainError):
    """Caller is neither requester nor addressee of the connection."""


class ConnectionWrongState(DomainError):
    """Operation is invalid for the connection's current state."""


class InvalidAuthToken(DomainError):
    """Token is unknown, expired, already consumed, or used for the wrong purpose."""


class AuthTokenRateLimited(DomainError):
    """The user has issued too many tokens for this purpose recently."""


class EmailChangePayloadMissing(DomainError):
    """Token has no payload — should be unreachable, but we surface it as 400."""


class SelfReportForbidden(DomainError):
    """A user attempted to report themselves (NEU-1162 §7.2).

    Its own error rather than `SelfConnectionForbidden`, which is named for the
    act it refuses and maps to a different status code.
    """


class InvalidCursor(DomainError):
    """Pagination cursor is malformed."""


class TooManyAttempts(DomainError):
    """A request budget rejected the request.

    Raised by the IP-keyed signup/login throttle (NEU-1160) and by the
    per-reporter report budget (NEU-1162) — named for the refusal rather than
    for the key, the same reason `config.Throttle` is.

    Carries the window in seconds so the router can set `Retry-After` without
    re-deriving the budget it just passed in.
    """

    def __init__(self, *, retry_after_seconds: int) -> None:
        super().__init__()
        self.retry_after_seconds = retry_after_seconds
