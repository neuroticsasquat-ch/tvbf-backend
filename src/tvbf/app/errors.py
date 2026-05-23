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


class InvalidCursor(DomainError):
    """Pagination cursor is malformed."""
