"""Root-logger configuration, in one place for every entrypoint.

The app calls this from `create_app`; the scheduled jobs in `tvbf.jobs` call it
from their `main()`. It lives here rather than in `main.py` so a cron process
does not build a FastAPI app and initialise Sentry as a side effect of wanting
log formatting.
"""

import logging


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        force=True,
    )
