"""The weekly recommendations pass — today only its `--dry-run` path (NEU-1105).

    python -m tvbf.jobs.weekly_recommendations --dry-run --user <uuid>

Compiles one user's taste payload, writes it to stdout, reports what it contains
on stderr, and exits **without calling a provider**. The pass itself — the
advisory lock, the hash gate, the call, resolution and storage — is M4's; this
module exists first because the payload has to be checked against real data
before any of it reaches a model.

**This is how the classification rules get verified** (project spec §10.2). Every
rule the payload rests on — the tier boundaries, the rating override, the 180-day
clause, the 50-cap, the weighted floor — was chosen from aggregate counts over
577 My Shows rows and 216 watched pairs. Running this against the real 522-show
account is what turns them from plausible into checked.

**CLI-only, deliberately no admin endpoint.** The payload is a user's complete
watch history; exposing it over HTTP, even behind the admin token, creates a
data-egress surface for something whose only consumer is a terminal.

## stdout carries the payload and nothing else

The reconciliation harness's rule (`jobs/reconcile.py`) applies for the same
reason: the artifact has to survive `ssh 'docker exec -i ...'` into a file, so
the counts go to stderr through the logger and stdout gets the canonical bytes.
The one byte of difference is the trailing newline, which is the terminal's
rather than the payload's — the hash is over `payload.json` without it.

## The token count is an estimate, and it is labelled as one

There is no offline tokenizer for DeepSeek here: `tiktoken` is not a dependency,
and it would be OpenAI's tokenizer answering a question about somebody else's
model. Asking the provider is exactly what this path exists not to do. So the
count is bytes over a **measured** ratio rather than a tokenization, and it is
reported as `~`.

`BYTES_PER_TOKEN` comes from NEU-1100's live measurement (2026-08-15,
`scripts/probe_deepinfra.py`): the 522-row account payload is 17,825 bytes and
came back as 6,748 input tokens. That figure includes the instruction, which is
M4's and does not exist yet, so the ratio **errs high on the payload alone** —
the right direction for a number whose two uses are context headroom and cost
sizing. `user_recommendation_set.input_tokens` is where the provider's exact
count gets recorded once there is a call to record it (§9).

**Exit codes: 0 = the payload compiled, 1 = it could not.** Below the floor is a
0 — the dry run answered the question, and the answer is one of the things worth
looking at. An unknown user, or an unset `RECOMMENDATION_MODEL`, is a 1.
"""

import argparse
import asyncio
import json
import logging
import sys
from uuid import UUID

from tvbf.app.repos import user_repo
from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging
from tvbf.recommendations.payload import (
    GENERATION_FLOOR,
    LIKED_WEIGHT,
    PROMPT_VERSION,
    TastePayload,
    build_payload,
)

log = logging.getLogger(__name__)

BYTES_PER_TOKEN = 2.64
"""Measured bytes per input token for this payload against DeepSeek (NEU-1100).

17,825 bytes of payload answered as 6,748 input tokens *including* the
instruction. See the module docstring for why an estimate at all and why this one
is deliberately the high side.
"""


def estimate_tokens(canonical_json: str) -> int:
    """About how many input tokens the payload will cost. Never exact."""
    return round(len(canonical_json.encode("utf-8")) / BYTES_PER_TOKEN)


def _report(user_id: UUID, model: str, payload: TastePayload) -> None:
    """What the payload holds, on stderr, for a person reading a terminal."""
    document = json.loads(payload.json)
    weighted = LIKED_WEIGHT * payload.liked_count + payload.interested_count

    log.info("user %s, model %s, prompt version %s", user_id, model, PROMPT_VERSION)
    log.info("payload hash %s", payload.hash)
    log.info(
        "%d bytes, ~%d tokens",
        len(payload.json.encode("utf-8")),
        estimate_tokens(payload.json),
    )
    log.info(
        # `not_liked` is read off the document rather than through an accessor
        # because nothing but this report wants it: it is exclusion signal and
        # contributes nothing to the floor (spec §5.4).
        "%d liked, %d not liked, %d interested; %d shows excluded from recommendations",
        payload.liked_count,
        len(document["not_liked"]),
        payload.interested_count,
        len(payload.excluded_show_ids),
    )
    verdict = "meets" if payload.meets_floor else "below"
    log.info(
        "%s the generation floor: (%d x %d) + %d = %d, floor %d",
        verdict,
        LIKED_WEIGHT,
        payload.liked_count,
        payload.interested_count,
        weighted,
        GENERATION_FLOOR,
    )


async def run(args: argparse.Namespace) -> int:
    """The whole job, minus argument parsing. Returns the process exit code.

    Split out from `main` so tests can await it on their own event loop —
    `main`'s `asyncio.run` would otherwise rebuild the loop under the shared
    engine's pooled connections.
    """
    model = get_settings().recommendation_model
    if not model:
        # Deliberately not defaulted (config.py): the model id is *in* the hash,
        # so a payload compiled against a guessed id is one a real run would not
        # match. Failing here beats printing a hash nothing will ever agree with.
        log.error("RECOMMENDATION_MODEL is unset, and it is part of the payload hash")
        return 1

    async with SessionLocal() as s:
        if await user_repo.get_by_id(s, args.user) is None:
            log.error("no user with id %s", args.user)
            return 1
        payload = await build_payload(s, user_id=args.user, model=model)

    # stdout carries the artifact and nothing else.
    sys.stdout.write(payload.json + "\n")
    _report(args.user, model, payload)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tvbf.jobs.weekly_recommendations")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compile one user's payload, print it, and exit without calling a provider",
    )
    parser.add_argument(
        "--user",
        type=UUID,
        help="the user whose payload to compile (required by --dry-run)",
    )
    args = parser.parse_args(argv)
    if not args.dry_run:
        # The bare invocation is the weekly pass, and it does not exist yet. An
        # empty run that exits 0 would read as "no user needed regenerating".
        parser.error("only --dry-run exists today; the weekly pass itself is M4")
    if args.user is None:
        parser.error("--dry-run needs --user <uuid>")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
