"""The weekly recommendations pass, and its `--dry-run` path.

    python -m tvbf.jobs.weekly_recommendations [--user <uuid>]
    python -m tvbf.jobs.weekly_recommendations --dry-run --user <uuid>

The bare invocation is the pass (NEU-1109), a Coolify scheduled task running
Sundays. `--dry-run` (NEU-1105) compiles one user's payload, writes it to
stdout, and exits **without calling a provider**.

Follows NEU-1008 like every other scheduled job here: **the process is the run
and the exit code is the result**, `configure_logging()` in `main()`. The
healthchecks.io deadman that covers the case Coolify cannot see — the task never
firing at all — is NEU-1111's, along with the schedule itself.

## There is no run table, and the guard is an advisory lock

`user_recommendation_set` already *is* a per-user run record carrying status,
timing, tokens and the raw response (project spec §10), so a second home for run
rows would mean a second stale-run cleanup, a second liveness guard and a second
status route for a job that finishes in seconds. Dropping the run table drops
`_reject_if_in_flight` with it, and that guard was doing real work: a manual
trigger during the cron would have both processes read the same stale hash, both
find the user dirty, and both spend a call.

`pg_try_advisory_lock` replaces it — one statement, no table, no staleness
heuristic, and it **releases when the connection dies**, which is exactly the
failure mode `INGEST_STALE_RUN_MINUTES` exists to paper over for the long-running
ingests. A second process finding it held logs and exits **0**: a concurrent pass
is not an error, and failing would have Coolify notify on a benign condition —
the same call `jobs/scheduled.run_scheduled_delta` makes for its in-flight guard.

## What each per-user outcome means

- **skipped** — the payload hashes identically to the one behind the user's
  current set, so nothing about their taste changed and the model would answer
  the same thing (§9.1). Costs a query, not a call.
- **`insufficient_history`** — below the weighted generation floor. A row with
  the hash and no recommendations. Note that such a user gets a **fresh row every
  week**, not one: NEU-1108 settled the gate on the newest *succeeded* set, so
  the hash it compares against is not this row's. That is deliberate there — a
  gate reading the newest set of any status would skip an unchanged user forever
  after one provider outage — and the cost of it here is ~52 rows a user a year
  and no calls at all, which is the half §5.4 actually cares about.
- **`failed`** — the call did not produce a usable answer. Isolated: logged,
  recorded, and the pass moves to the next user.
- **`no_matches`** — the model answered and *nothing* resolved. Deliberately not
  `succeeded`: reads take the newest succeeded set, so this leaves last week's
  recommendations standing rather than silently emptying the section, and makes
  a systematic resolution break visible instead of looking like a quiet week.
- **`succeeded`** — at least one title resolved. Resolution failures are not run
  failures (§10.1); 25 titles returning 19 rows is a success with 19 rows, on
  exactly the terms NEU-1043 treats unmatched shows as expected output.

## Exit codes: 0 = every user the pass reached ended in a non-failure

**1 if any user failed**, which is stricter than most jobs here on purpose: at
3-5 accounts one failure is 20-33% of the user base, and `llm/client` has
already walked its backoff curve, so a failure that survives it is worth waking
up for. `insufficient_history` and `no_matches` are not failures — they are the
pass working.

`CONSECUTIVE_FAILURE_LIMIT` aborts the rest of the pass, on the ingest's
consecutive-failure precedent: a provider that has failed three users running is
not going to serve the fourth, and the calls are what cost money.

## Sequential per user, with a written-down threshold for changing it

Cost is `users x latency` — 0.6 minutes at 5 users, ~11 at 100 and ~23 at 200
against NEU-1100's measured 6.87s median. That stops being right somewhere
around **100-200 users**, and the fix then is a bounded semaphore around the
call, not a rewrite. Writing the threshold down beats pre-building for it, and
the shared DeepInfra budget (NEU-1099) is what makes the eventual change safe
rather than dangerous.
"""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import (
    SET_STATUS_FAILED,
    SET_STATUS_INSUFFICIENT_HISTORY,
    SET_STATUS_NO_MATCHES,
    SET_STATUS_SUCCEEDED,
)
from tvbf.app.repos import recommendation_repo, user_repo
from tvbf.config import Settings, get_settings
from tvbf.db import SessionLocal, engine
from tvbf.llm.client import OpenAICompatClient
from tvbf.llm.registry import DEEPINFRA
from tvbf.llm.types import LLMResponse, LLMResponseInvalid, UnsupportedPromptError
from tvbf.logging_config import configure_logging
from tvbf.recommendations import resolution
from tvbf.recommendations.payload import (
    GENERATION_FLOOR,
    INTERESTED_CAP,
    LIKED_WEIGHT,
    PROMPT_VERSION,
    TastePayload,
    build_payload,
)
from tvbf.recommendations.prompt import (
    RECOMMENDATION_COUNT,
    Suggestion,
    build_prompt,
    describe_dropped,
    parse_suggestions,
)

log = logging.getLogger(__name__)

BYTES_PER_TOKEN = 2.64
"""Measured bytes per input token for this payload against DeepSeek (NEU-1100).

17,825 bytes of payload answered as 6,748 input tokens *including* the
instruction. See `--dry-run`'s notes below for why an estimate at all and why
this one is deliberately the high side.
"""

ADVISORY_LOCK_KEY = 0x7476_6266_7265_6301
"""The fixed `int64` this pass takes `pg_try_advisory_lock` on.

Arbitrary but **constant forever**: the bytes spell `tvbfrec` with a version
byte, which is only a mnemonic — what matters is that every process running this
job asks for the same number, so a value derived from anything (a hash of a
name, a config value) is a way for two deployments to disagree silently. Postgres
advisory locks share one keyspace per database, so a second job wanting one picks
a different constant here rather than reusing this one.
"""

CONSECUTIVE_FAILURE_LIMIT = 3
"""How many users may fail in a row before the pass gives up on the rest.

Hardcoded rather than a setting, and that is the decision: a setting implies a
tuning need that will not materialize at 3-5 users, and every setting is a thing
to get wrong. Same shape as `INGEST_CONSECUTIVE_FAILURE_THRESHOLD`, one level
smaller because the unit here is a user rather than a show.
"""


def estimate_tokens(canonical_json: str) -> int:
    """About how many input tokens the payload will cost. Never exact."""
    return round(len(canonical_json.encode("utf-8")) / BYTES_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class UserOutcome:
    """How one user's turn ended, as `run_pass` needs to hear it.

    `status` is the `user_recommendation_set` status that was written, or `None`
    when the regeneration gate skipped the user and no row was written at all.
    A failure is not represented here — it arrives as an exception, because a
    turn that raised has no outcome to report and `run_pass` is what decides
    whether the pass survives it.
    """

    status: str | None
    unresolved: int = 0


@dataclass
class PassResult:
    """What one run of the pass did, per outcome. Its `ok` is the exit code.

    Counted rather than derived from the database afterwards: a set written by
    *this* run and a set that happened to be newest are different claims, and
    only the first says whether the process succeeded.
    """

    skipped: int = 0
    insufficient: int = 0
    succeeded: int = 0
    no_matches: int = 0
    failed: int = 0
    unresolved: int = 0
    aborted: bool = False

    @property
    def ok(self) -> bool:
        """Whether the process should exit 0.

        `insufficient_history` and `no_matches` are not failures — they are the
        pass working — so only a user whose turn raised, or an abandoned run,
        makes this false.
        """
        return self.failed == 0 and not self.aborted

    def record(self, outcome: UserOutcome) -> None:
        """Tally one completed turn.

        Deliberately exhaustive rather than falling through to a default: a
        status this does not recognise — `failed` most of all, which reaches
        `run_pass` as an exception and never as an outcome — would otherwise be
        counted as something it is not, and the counts are what the summary line
        and the exit code are read off.
        """
        self.unresolved += outcome.unresolved
        if outcome.status is None:
            self.skipped += 1
        elif outcome.status == SET_STATUS_SUCCEEDED:
            self.succeeded += 1
        elif outcome.status == SET_STATUS_NO_MATCHES:
            self.no_matches += 1
        elif outcome.status == SET_STATUS_INSUFFICIENT_HISTORY:
            self.insufficient += 1
        else:
            raise ValueError(f"a user's turn cannot end as {outcome.status!r}")


@asynccontextmanager
async def _advisory_lock() -> AsyncIterator[bool]:
    """Hold `ADVISORY_LOCK_KEY` for the block, or report that somebody else does.

    On its own connection, checked out for the whole pass. A session-level
    advisory lock belongs to a *connection*, so taking it on a pooled session
    that later commits would hand the lock's lifetime to the pool rather than to
    this job. The explicit unlock is belt-and-braces — closing the connection
    releases it, which is the property that makes this guard need no staleness
    heuristic in the first place.
    """
    async with engine.connect() as conn:
        held = bool(
            (await conn.execute(select(func.pg_try_advisory_lock(ADVISORY_LOCK_KEY)))).scalar_one()
        )
        try:
            yield held
        finally:
            if held:
                await conn.execute(select(func.pg_advisory_unlock(ADVISORY_LOCK_KEY)))


def _client(settings: Settings, model: str) -> OpenAICompatClient:
    """The provider client this pass calls, on the shared DeepInfra budget.

    Constructed once for the whole pass rather than per user: one `httpx`
    connection pool and one limiter across the run is what makes "sequential per
    user" cost `users x latency` and nothing else.
    """
    return OpenAICompatClient(
        provider=DEEPINFRA,
        api_key=settings.deepinfra_api_key,
        model=model,
        rate_calls=settings.deepinfra_rate_limit_requests,
        rate_window=settings.deepinfra_rate_limit_window_seconds,
    )


async def _ask(
    client: OpenAICompatClient, payload: TastePayload
) -> tuple[LLMResponse, list[Suggestion], list[Any]]:
    """One user's call **and the reading of its answer**, retried exactly once.

    `LLMResponseInvalid` only — a body that did not decode, decoded to something
    that is not an object, or decoded to an object that is not the §7 output
    contract. Malformed model JSON is frequently a one-off and a second call
    costs a fraction of a cent.

    **The parse is inside the retried block, and that is the whole reason this
    function returns a triple** rather than just the response. `parse_suggestions`
    reaches the same verdict as the client for a body missing `recommendations`,
    and a retry that covered only the client's half would leave the identical
    failure — "a response arrived and could not be believed" — retried or not
    depending on which layer noticed it.

    `LLMRequestFailed` is deliberately **not** retried: the client has already
    walked its backoff curve, so a failure that reaches here has survived it and
    asking again immediately buys the same failure while spending this user's
    turn on it (`llm/types`).
    """
    prompt = build_prompt(payload.json)
    try:
        return _read(await client.complete_json(prompt))
    except LLMResponseInvalid as exc:
        log.warning("the model's answer could not be believed (%s); asking once more", exc)
        return _read(await client.complete_json(prompt))


def _read(response: LLMResponse) -> tuple[LLMResponse, list[Suggestion], list[Any]]:
    """The response beside what §7's contract says it holds."""
    suggestions, dropped = parse_suggestions(response.parsed)
    return response, suggestions, dropped


async def _resolve_all(
    db: AsyncSession, suggestions: Sequence[Suggestion], payload: TastePayload
) -> tuple[list[recommendation_repo.NewRecommendation], list[str]]:
    """Resolved rows in the model's own order, and the titles that resolved to nothing.

    Two filters ride along, and both are the caller's rather than
    `resolution.resolve`'s (see that module):

    **The exclusions.** Never recommend a show the user already has a record
    for. `excluded_show_ids` is that set and is the same object the payload was
    built from, so no second query is needed — the prompt states the rule too,
    and this is the half that guarantees it (§8).

    **The duplicates.** Two model-authored titles can resolve to one show, and
    `uq_user_recommendation_set_rank` would not catch it — a set holding the same
    show twice is two cards of the same thing in a grid of twelve.
    """
    rows: list[recommendation_repo.NewRecommendation] = []
    unresolved: list[str] = []
    seen: set[int] = set()
    for suggestion in suggestions:
        resolved = await resolution.resolve(
            db, title=suggestion.title, year=suggestion.release_year
        )
        if resolved is None:
            unresolved.append(f"{suggestion.title} ({suggestion.release_year})")
            continue
        # Logged rather than dropped in silence: if the model starts echoing the
        # user's own library back, the set shrinks toward `no_matches` and these
        # are the only lines that would say why.
        if resolved.show_id in payload.excluded_show_ids:
            log.info(
                "dropped %r — the user already has show %d", suggestion.title, resolved.show_id
            )
            continue
        if resolved.show_id in seen:
            log.info(
                "dropped %r — an earlier recommendation already named show %d",
                suggestion.title,
                resolved.show_id,
            )
            continue
        seen.add(resolved.show_id)
        rows.append(
            recommendation_repo.NewRecommendation(
                show_id=resolved.show_id,
                reason=suggestion.reason,
                matched_via=resolved.matched_via,
            )
        )
    return rows, unresolved


async def _generate_for_user(
    db: AsyncSession,
    *,
    client: OpenAICompatClient,
    user_id: UUID,
    model: str,
) -> UserOutcome:
    """One user's whole turn, from compiling their payload to writing their set.

    Raises whatever the call or the database raised; `run_pass` is what isolates
    a failure to this user. Nothing here commits — the caller does, so that a set
    and its rows land together or not at all.
    """
    payload = await build_payload(db, user_id=user_id, model=model)
    compiled = json.loads(payload.json)

    current = await recommendation_repo.get_current_set(db, user_id=user_id)
    if current is not None and current.payload_hash == payload.hash:
        # §9.1: the payload *is* the model's entire input, so identical bytes
        # mean identical output. A user who changed nothing must not see their
        # recommendations churn week to week.
        log.info("user %s is unchanged since their current set; skipping", user_id)
        return UserOutcome(status=None)

    if not payload.meets_floor:
        weighted = LIKED_WEIGHT * payload.liked_count + payload.interested_count
        log.info(
            "user %s is below the generation floor ((%d x %d) + %d = %d, floor %d)",
            user_id,
            LIKED_WEIGHT,
            payload.liked_count,
            payload.interested_count,
            weighted,
            GENERATION_FLOOR,
        )
        await recommendation_repo.write_set(
            db,
            user_id=user_id,
            status=SET_STATUS_INSUFFICIENT_HISTORY,
            payload_hash=payload.hash,
            prompt_version=PROMPT_VERSION,
            model=model,
            compiled_payload=compiled,
        )
        return UserOutcome(status=SET_STATUS_INSUFFICIENT_HISTORY)

    log.info(
        "asking for %d recommendations for user %s (~%d input tokens)",
        RECOMMENDATION_COUNT,
        user_id,
        estimate_tokens(payload.json),
    )
    response, suggestions, dropped = await _ask(client, payload)
    if dropped:
        # §7: a recommendation without a `release_year` is dropped, because the
        # year is the only disambiguator resolution has. Logged rather than
        # counted silently — it is preserved in `raw_response` either way.
        log.warning(
            "dropped %d of %d entries that were not the output contract: %s",
            len(dropped),
            len(suggestions) + len(dropped),
            describe_dropped(dropped),
        )

    rows, unresolved = await _resolve_all(db, suggestions, payload)
    if unresolved:
        # A resolution failure is useful signal, not a defect (§8): an unmatched
        # title is either a hallucination or a genuine catalog gap, and both
        # belong in the logs.
        log.info("user %s: %d titles resolved to nothing: %s", user_id, len(unresolved), unresolved)

    status = SET_STATUS_SUCCEEDED if rows else SET_STATUS_NO_MATCHES
    await recommendation_repo.write_set(
        db,
        user_id=user_id,
        status=status,
        payload_hash=payload.hash,
        prompt_version=PROMPT_VERSION,
        model=model,
        compiled_payload=compiled,
        raw_response=response.parsed,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        recommendations=rows,
    )
    if rows:
        log.info(
            "user %s: %d of %d recommendations stored (%d input / %d output tokens)",
            user_id,
            len(rows),
            len(suggestions),
            response.input_tokens,
            response.output_tokens,
        )
    else:
        log.warning(
            "user %s: nothing the model named resolved — recorded as %s, so their "
            "current recommendations stand",
            user_id,
            SET_STATUS_NO_MATCHES,
        )
    return UserOutcome(status=status, unresolved=len(unresolved))


async def _record_failure(user_id: UUID, model: str) -> None:
    """Write a `failed` set for a user whose turn raised, on its own session.

    Its own session because the one that raised is in an unknown state — a
    `SQLAlchemyError` leaves the transaction unusable, and the row recording the
    failure is the only place a failure becomes visible at 3-5 users. A payload
    is recompiled rather than threaded through: the failure may have happened
    *while* compiling it, and there is no set row without a `compiled_payload`
    and a `payload_hash`.

    Best-effort by construction. If this raises too, the pass's own log line is
    what is left, and that is the right trade against a job that dies while
    reporting that something else died.
    """
    async with SessionLocal() as db:
        payload = await build_payload(db, user_id=user_id, model=model)
        await recommendation_repo.write_set(
            db,
            user_id=user_id,
            status=SET_STATUS_FAILED,
            payload_hash=payload.hash,
            prompt_version=PROMPT_VERSION,
            model=model,
            compiled_payload=json.loads(payload.json),
        )
        await db.commit()


async def run_pass(settings: Settings, *, user_id: UUID | None = None) -> PassResult:
    """The weekly pass over every user, or over one.

    `user_id` narrows the work list to a single account. It is a debugging
    affordance and the seam `POST /admin/recommendations` (NEU-1110) will be
    written against; the schedule always calls it with nothing.

    The advisory lock is **not** taken here — `run` takes it around this, so a
    caller that already holds it (an in-process admin trigger) is not deadlocked
    by its own guard.
    """
    model = settings.recommendation_model
    if not model:
        raise ValueError("RECOMMENDATION_MODEL is not set — there is no model to call")

    async with SessionLocal() as db:
        user_ids = [user_id] if user_id is not None else await user_repo.list_ids(db)

    result = PassResult()
    consecutive_failures = 0
    log.info("weekly recommendations pass over %d user(s), model %s", len(user_ids), model)

    async with _client(settings, model) as client:
        for candidate in user_ids:
            try:
                async with SessionLocal() as db:
                    outcome = await _generate_for_user(
                        db, client=client, user_id=candidate, model=model
                    )
                    await db.commit()
            except UnsupportedPromptError:
                # Not a call outcome and not this user's fault: the instruction
                # itself is unusable, so every remaining user would fail the same
                # way. Raising beats writing `CONSECUTIVE_FAILURE_LIMIT` rows
                # blaming users for a programming error.
                raise
            except Exception as exc:
                # Deliberately every exception, not only `LLMError`. Per-user
                # isolation is about the *user*, not about which layer failed:
                # a `SQLAlchemyError` while resolving one account must not cost
                # the other four their week (§10.1).
                log.exception("user %s failed: %s", candidate, exc)
                result.failed += 1
                consecutive_failures += 1
                try:
                    await _record_failure(candidate, model)
                except Exception:
                    log.exception("could not record the failed set for user %s", candidate)
                if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                    log.error(
                        "%d users failed in a row; abandoning the rest of the pass",
                        consecutive_failures,
                    )
                    result.aborted = True
                    break
                continue
            result.record(outcome)
            consecutive_failures = 0

    log.info(
        "pass finished: %d generated, %d skipped as unchanged, %d below the floor, "
        "%d resolved nothing, %d failed; %d titles resolved to nothing in total%s",
        result.succeeded,
        result.skipped,
        result.insufficient,
        result.no_matches,
        result.failed,
        result.unresolved,
        " (aborted)" if result.aborted else "",
    )
    return result


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
        #
        # INTERESTED reports what the cap dropped, because the rows cannot say:
        # exactly 50 reads the same whether the user bookmarked 50 shows or 300,
        # and the cap is one of the rules this run exists to check.
        "%d liked, %d not liked, %d interested (of %d before the %d-row cap); "
        "%d shows excluded from recommendations",
        payload.liked_count,
        len(document["not_liked"]),
        payload.interested_count,
        payload.interested_before_cap,
        INTERESTED_CAP,
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


async def run_dry_run(user_id: UUID) -> int:
    """Compile one user's payload, print it, and exit without calling anything.

    NEU-1105. **This is how the classification rules get verified** (project spec
    §10.2): every rule the payload rests on — the tier boundaries, the rating
    override, the 180-day clause, the 50-cap, the weighted floor — was chosen
    from aggregate counts, and running this against the real 522-show account is
    what turns them from plausible into checked.

    **CLI-only, deliberately no admin endpoint.** The payload is a user's
    complete watch history; exposing it over HTTP, even behind the admin token,
    creates a data-egress surface for something whose only consumer is a
    terminal.

    ## stdout carries the payload and nothing else

    The reconciliation harness's rule (`jobs/reconcile.py`) applies for the same
    reason: the artifact has to survive `ssh 'docker exec -i ...'` into a file,
    so the counts go to stderr through the logger and stdout gets the canonical
    bytes. The one byte of difference is the trailing newline, which is the
    terminal's rather than the payload's — the hash is over `payload.json`
    without it.

    ## The token count is an estimate, and it is labelled as one

    There is no offline tokenizer for DeepSeek here: `tiktoken` is not a
    dependency, and it would be OpenAI's tokenizer answering a question about
    somebody else's model. Asking the provider is exactly what this path exists
    not to do. So the count is bytes over a **measured** ratio rather than a
    tokenization, and it is reported as `~`. `BYTES_PER_TOKEN` errs high on the
    payload alone, which is the right direction for a number whose two uses are
    context headroom and cost sizing.

    **Exit codes: 0 = the payload compiled, 1 = it could not.** Below the floor
    is a 0 — the dry run answered the question, and the answer is one of the
    things worth looking at. An unknown user, or an unset `RECOMMENDATION_MODEL`,
    is a 1.
    """
    model = get_settings().recommendation_model
    if not model:
        # Deliberately not defaulted (config.py): the model id is *in* the hash,
        # so a payload compiled against a guessed id is one a real run would not
        # match. Failing here beats printing a hash nothing will ever agree with.
        log.error("RECOMMENDATION_MODEL is unset, and it is part of the payload hash")
        return 1

    async with SessionLocal() as s:
        if await user_repo.get_by_id(s, user_id) is None:
            log.error("no user with id %s", user_id)
            return 1
        payload = await build_payload(s, user_id=user_id, model=model)

    # stdout carries the artifact and nothing else.
    sys.stdout.write(payload.json + "\n")
    _report(user_id, model, payload)
    return 0


async def run(args: argparse.Namespace) -> int:
    """The whole job, minus argument parsing. Returns the process exit code.

    Split out from `main` so tests can await it on their own event loop —
    `main`'s `asyncio.run` would otherwise rebuild the loop under the shared
    engine's pooled connections.
    """
    if args.dry_run:
        return await run_dry_run(args.user)

    async with _advisory_lock() as held:
        if not held:
            # Somebody triggered the pass by hand minutes before the schedule
            # fired, or the last one has not finished. Exit 0: this process did
            # nothing wrong, and failing would have Coolify notify on a benign
            # condition — the call `jobs/scheduled` already makes for its own
            # in-flight guard.
            log.info("another weekly recommendations pass holds the lock; nothing to do")
            return 0

        settings = get_settings()
        if not settings.recommendation_model or not settings.deepinfra_api_key:
            # Checked before the work list rather than at the first call: a pass
            # that compiles five payloads and then discovers it cannot
            # authenticate has spent five users' turns learning it.
            log.error("RECOMMENDATION_MODEL and DEEPINFRA_API_KEY must both be set")
            return 1

        result = await run_pass(settings, user_id=args.user)
    return 0 if result.ok else 1


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
        help="one user to run (required by --dry-run; the pass covers everybody without it)",
    )
    args = parser.parse_args(argv)
    if args.dry_run and args.user is None:
        parser.error("--dry-run needs --user <uuid>")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
