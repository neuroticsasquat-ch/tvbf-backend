"""A unique, valid handle for a test that seeds `app.user` directly.

`uq_user_handle` is a real constraint, so every seeded row needs a distinct
value; deriving one from the display name would collide the moment two rows
share a name, which several of these tests do on purpose. Not derived from the
email either, for the same reason — `a@example.com` and `a@other.com` share a
local part.
"""

from uuid import uuid4


def new_handle() -> str:
    """A fresh handle matching `^[a-z][a-z0-9_]{2,29}$`."""
    return f"u{uuid4().hex[:12]}"
