"""Root-logger configuration, in one place for every entrypoint.

The app calls this from `create_app`; the scheduled jobs in `tvbf.jobs` call it
from their `main()`. It lives here rather than in `main.py` so a cron process
does not build a FastAPI app and initialise Sentry as a side effect of wanting
log formatting.
"""

import logging
import re

_PUSH_TOKEN = re.compile(r"(/api/push/)[^/?#&\s'\"]+")


def redact_push_token(text: str) -> str:
    """Replace the token in any Uptime Kuma `/api/push/<token>` path with `REDACTED`.

    The token is the push monitor's only credential: whoever holds it can push
    `status=up` and hide a failed job (NEU-1617).
    """
    return _PUSH_TOKEN.sub(r"\1REDACTED", text)


class RedactingFilter(logging.Filter):
    """Scrubs push tokens out of every record, whoever emitted it.

    `httpx` logs `HTTP Request: GET <full url>` at INFO on every request, so each
    deadman ping wrote its token to the job's output. Rewrites `msg` and drops
    `args` because httpx passes the URL as an argument; collapsing them into the
    final message is the only way to scrub both in one pass.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_push_token(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        force=True,
    )
    # On the handlers, not the root logger: a logger's filter is not consulted for
    # records propagating up from its children, so `httpx`'s would sail past it.
    redactor = RedactingFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
