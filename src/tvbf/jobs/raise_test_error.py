"""A scheduled-job crash on purpose, to prove the jobs' error path (NEU-1622).

    python -m tvbf.jobs.raise_test_error

The job-side twin of `POST /admin/debug/raise-test-error`: the ops runbook runs
it as a Coolify task (NEU-1625) to show that a job dying with an unhandled
exception reaches Bugsink, tagged `job=raise_test_error`, and from there Discord
`#alerts` (NEU-1597). It goes through `scheduled_main` rather than raising at
module level because that handler is the path real job crashes take. It has no
deadman, so the only signals are the event and exit code 1.
"""

import sys

from tvbf.config import Settings
from tvbf.jobs.scheduled import scheduled_main


class JobTestError(RuntimeError):
    """Its own type, so Bugsink groups every trigger into one issue that no real
    failure can be mistaken for."""


async def raise_test_error(settings: Settings) -> bool:
    raise JobTestError("tvbf.jobs.raise_test_error: deliberate test error (NEU-1622)")


def main() -> int:
    return scheduled_main(
        runner=raise_test_error,
        healthcheck_url=lambda s: None,
        name="raise test error",
        job="raise_test_error",
    )


if __name__ == "__main__":
    sys.exit(main())
