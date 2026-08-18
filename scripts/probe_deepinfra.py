"""Measure DeepSeek-on-DeepInfra against the live API: model id, JSON mode, latency, contract.

Four things the recommendations project (project spec §6–§7, §10) rests on are
unmeasured, and each of them fails in a way nothing below production would catch:

1. **The exact model id.** `RECOMMENDATION_MODEL` is deliberately undefaulted
   because an id asserted from memory is a non-retryable 404 that looks like an
   outage. Read the catalog, then confirm a real call succeeds.
2. **Whether `response_format: {"type": "json_object"}` is honoured**, and
   whether the recorded constraint still holds — upmovies measured DeepSeek
   answering `400` when `response_format` is set and the word "json" appears
   nowhere in the prompt. `llm/client.py:_to_wire` enforces that constraint, and
   two of its strictnesses (case-sensitive, `system` only) were chosen as the
   safe direction of an unknown rather than measured. This tells us what the
   provider actually does.
3. **Round-trip latency for a payload the size of the 522-show account.** §10
   says the weekly pass stays sequential until somewhere around 100–200 users;
   that threshold is `users × latency`, so it is a number nobody has yet.
4. **Whether the response reliably parses as the output contract** across a few
   runs, which is what the single malformed-response retry (`types.py`) is
   priced against.

Run inside the container, with `DEEPINFRA_API_KEY` set in `.env`:

    docker compose exec tvbf-backend python scripts/probe_deepinfra.py

Costs one catalog listing, one small call per candidate model, four small
JSON-mode calls, and `--runs` full-size calls (3 by default) — cents, not
dollars. Record what it prints in `CLAUDE.md`.

**What it prints does not by itself choose the model, and NEU-1180 is the bill
for reading it as though it did.** Measurement 3 ranks ids that all work; it
never varies the payload's *size*, so it cannot see the failure that actually
bit — a model that returns 25 titles the user already has, at the library size
we already have. `scripts/probe_deepinfra_capacity.py` is the axis that decides
`RECOMMENDATION_MODEL`; this script says whether the id exists, answers JSON,
and how long a pass will take once it is chosen.

Two deliberate departures from `scripts/probe_tmdb_*.py`. The JSON-mode and
catalog requests are made with **raw `httpx`**, not `OpenAICompatClient`: the
client refuses to send a prompt without the word "json" by design, so measuring
what the provider does with one is only possible below it, and `/models` is not
on its call surface at all. The full-size calls in measurement 4 do go through
the client, because latency and contract are properties of the request the job
will actually make. Section numbers below and in the printed output are the four
measurements above; `§` always means a section of the *project spec*.

Raw calls spend no rate budget, which is safe here for the reason it would not
be in a job: a few dozen sequential calls against a 5/s ceiling. Note this is
wider than `probe_tmdb_append_limit.py`'s precedent, which reaches past the
client's `append` planning but still spends the TMDB budget — here the budget is
skipped outright, and only the volume makes that acceptable.

The taste payload is built from **real `catalog.show` titles**, not synthetic
ones. A model asked about 522 invented shows answers a different question, and
both the latency and the contract reliability being measured are properties of
the answer.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from typing import Any

import httpx
from sqlalchemy import select

from tvbf.catalog.models import Show
from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.llm.client import OpenAICompatClient
from tvbf.llm.registry import DEEPINFRA, base_url_for
from tvbf.llm.retry import DEFAULT_RETRY_POLICY, RetryPolicy
from tvbf.llm.types import LLMError, Prompt

# The heaviest real account in the measured baseline (project spec §4): 522
# My Shows rows, of which §5.3 caps `interested` at 50. The remainder is split
# the way that account's watch data does — most of it started, a little of it
# abandoned — so the row count is the ceiling that account can produce.
ACCOUNT_SHOWS = 522
INTERESTED_CAP = 50
# The remaining 472 rows split into NOT LIKED and LIKED. The baseline gives no
# exact split — it counts 216 (user, show) pairs with a watch across three
# accounts, not this account's tiers — so the number is a plausible minority
# rather than a measurement, and the payload's *size* is what measurements 3
# and 4 depend on. Named anyway: an unnamed 72 in a slice looks like a fact.
NOT_LIKED_ROWS = 72

# What the job will ask for (project spec §7): 25 recommendations, 12 displayed.
RECOMMENDATIONS_REQUESTED = 25

# Where §10's sequential-versus-concurrent threshold is written down. Printed
# against the measured latency so the arithmetic is in the output rather than
# left for the reader.
THRESHOLD_USERS = (5, 100, 200)


_CONTRACT_SHAPE = (
    '{"recommendations": [{"title": "...", "release_year": 2019, "reason": "one sentence"}]}'
)


def _system_prompt(word: str | None = "json") -> str:
    """The instruction the job sends, with the JSON-mode word under control.

    `word=None` drops it entirely — that is the request `_to_wire` refuses to
    build and the one the provider is documented to reject. Nothing else in the
    instruction spells "json", so the word really is the only variable.
    """
    return (
        "You recommend television series. The user's viewing history is given as "
        "columnar data grouped by how they felt about each show. Recommend "
        f"{RECOMMENDATIONS_REQUESTED} series they have not seen. Answer with a single "
        f"{word or 'structured'} object of the form {_CONTRACT_SHAPE}. Every "
        "recommendation needs a release_year."
    )


async def _fetch_titles(limit: int) -> list[tuple[str, int | None]]:
    """Real show titles and premiere years, best-known first."""
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Show.name, Show.first_air_date)
            .where(Show.first_air_date.is_not(None))
            .order_by(Show.vote_count.desc().nulls_last(), Show.id)
            .limit(limit)
        )
        return [(name, air_date.year if air_date else None) for name, air_date in rows.all()]


def _taste_payload(titles: list[tuple[str, int | None]]) -> dict[str, Any]:
    """The columnar taste payload of project spec §5.3, at account scale."""
    interested = titles[:INTERESTED_CAP]
    not_liked = titles[INTERESTED_CAP : INTERESTED_CAP + NOT_LIKED_ROWS]
    liked = titles[INTERESTED_CAP + NOT_LIKED_ROWS :]
    return {
        "columns": ["title", "year", "pct", "stars"],
        "liked": [[name, year, 100, None] for name, year in liked],
        "not_liked": [[name, year, 12, None] for name, year in not_liked],
        "interested": [[name, year, 0, None] for name, year in interested],
    }


# --------------------------------------------------------------------------
# 1 & 2. the model id: what the catalog offers, and what answers a call
# --------------------------------------------------------------------------


async def _list_models(raw: httpx.AsyncClient) -> list[str]:
    print("=== 1. which DeepSeek models does the catalog offer? ===\n")
    try:
        response = await raw.get("/models")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"could not read the model catalog: {exc}\n")
        return []
    ids = sorted(entry.get("id", "") for entry in response.json().get("data", []))
    deepseek = [model_id for model_id in ids if "deepseek" in model_id.lower()]
    print(f"catalog: {len(ids)} models, {len(deepseek)} of them DeepSeek\n")
    for model_id in deepseek:
        print(f"  {model_id}")
    print()
    return deepseek


async def _chat(
    raw: httpx.AsyncClient, model: str, system: str, user: str
) -> tuple[httpx.Response | None, float, str | None]:
    """One chat-completions request, and how long it took.

    Every raw request in this file goes through here so that
    `response_format` — the field under measurement in §3 — is written **once**.
    Two copies of the request body is where a divergence would hide, and it
    would hide inside the instrument rather than in what it measures.

    Returns the response (None on a transport error), the elapsed seconds, and
    the transport error's text if there was one.
    """
    started = time.monotonic()
    try:
        response = await raw.post(
            "/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": 512,
                "response_format": {"type": "json_object"},
            },
        )
    except httpx.HTTPError as exc:
        return None, time.monotonic() - started, str(exc)
    return response, time.monotonic() - started, None


async def _smoke(raw: httpx.AsyncClient, model: str) -> tuple[bool, float, str]:
    """One tiny real call. A catalog entry is not proof the id serves a request."""
    response, elapsed, error = await _chat(
        raw, model, 'Answer with the json object {"ok": true}.', "ok?"
    )
    if response is None:
        return False, elapsed, f"transport error: {error}"
    if response.status_code != 200:
        return False, elapsed, f"HTTP {response.status_code}: {response.text[:160]}"
    content = response.json()["choices"][0]["message"]["content"]
    return True, elapsed, f"200 OK in {elapsed:5.2f}s, said {content!r}"


async def _confirm_models(raw: httpx.AsyncClient, candidates: list[str]) -> list[str]:
    """Smoke every candidate, fastest first.

    **Every candidate, with no cap.** A cap that stops short of the catalog is
    how this probe's own headline finding became unreproducible from its own
    defaults: the catalog is alphabetical, and the first run capped at six never
    reached the id it ended up recommending.
    """
    print("=== 2. does a real call to each candidate succeed? ===\n")
    working: list[tuple[float, str]] = []
    for model in candidates:
        ok, elapsed, detail = await _smoke(raw, model)
        print(f"{model:<44} {detail}")
        if ok:
            working.append((elapsed, model))
    print(f"\n{len(working)} of {len(candidates)} candidates answered.\n")
    # **This ordering does not rank models for the real workload, and must not
    # be read as if it did.** Measured 2026-08-15: `V4-Pro-0813` and `V4-Flash`
    # finish "ok?" within half a second of each other and do not hold their
    # order across sessions, while on the 522-show payload one is 2.5-3x the
    # other and holds it every run. What a trivial
    # prompt *does* separate is a reasoning model from a chat one — R1 spends
    # seconds thinking before its first visible token — and that is the
    # distinction deciding whether §4 can complete at all. So the fastest
    # responder is a safe **default** for §4 and nothing more; pass `--model`
    # (repeatably) to measure the ids you are actually choosing between.
    working.sort()
    return [model for _, model in working]


# --------------------------------------------------------------------------
# 3. JSON mode and the "json" constraint
# --------------------------------------------------------------------------


async def _json_mode_case(
    raw: httpx.AsyncClient, model: str, label: str, system: str, user: str
) -> None:
    response, _, error = await _chat(raw, model, system, user)
    if response is None:
        print(f"{label:<46} transport error: {error}")
        return
    if response.status_code != 200:
        print(f"{label:<46} HTTP {response.status_code}: {response.text[:120]}")
        return
    content = response.json()["choices"][0]["message"]["content"] or ""
    try:
        parsed = json.loads(content)
    except ValueError:
        # The prefix, not just the verdict: a reasoning model answers 200 with a
        # `<think>` block ahead of its JSON, which fails to decode for a reason
        # that has nothing to do with the constraint under measurement. Measured
        # 2026-08-15 on `DeepSeek-R1-0528`, where it made all four rows read
        # identically and said nothing about any of them.
        print(f"{label:<46} 200 OK, content did NOT decode: {content[:60]!r}")
        return
    shape = type(parsed).__name__
    print(f"{label:<46} 200 OK, content decoded as {shape}")


async def _probe_json_mode(raw: httpx.AsyncClient, model: str) -> None:
    print("=== 3. is JSON mode honoured, and what does the 'json' word do? ===\n")
    ask = "Name one television series."
    await _json_mode_case(
        raw,
        model,
        'lowercase "json" in the system message',
        _system_prompt("json"),
        ask,
    )
    await _json_mode_case(
        raw,
        model,
        'no "json" anywhere',
        _system_prompt(None),
        ask,
    )
    await _json_mode_case(
        raw,
        model,
        'uppercase "JSON" only',
        _system_prompt("JSON"),
        ask,
    )
    await _json_mode_case(
        raw,
        model,
        '"json" in the user message only',
        _system_prompt(None),
        f"{ask} Answer as a json object.",
    )
    print(
        "\nRow two is the recorded constraint: a 400 reproduces it, a 200 says this "
        "model does not\nenforce it and that `client._to_wire`'s guard is stricter "
        "than the provider. Rows three\nand four say the same about that guard's "
        "case-sensitivity and its system-only reading.\nStricter is the safe "
        "direction — the guard costs a loud local error, the 400 it stops costs\na "
        "user their week — but it should be stricter knowingly.\n"
    )


# --------------------------------------------------------------------------
# 4. latency and the output contract, at account scale
# --------------------------------------------------------------------------


def _contract_verdict(parsed: dict[str, Any]) -> str:
    """How well one response matches the output contract of project spec §7."""
    recommendations = parsed.get("recommendations")
    if not isinstance(recommendations, list):
        keys = sorted(parsed)[:6]
        return f"NO 'recommendations' list (top-level keys: {keys})"
    usable = 0
    missing_year = 0
    for entry in recommendations:
        if not isinstance(entry, dict) or not isinstance(entry.get("title"), str):
            continue
        if not isinstance(entry.get("release_year"), int):
            missing_year += 1
            continue
        if isinstance(entry.get("reason"), str):
            usable += 1
    return (
        f"{len(recommendations)} returned, {usable} usable, "
        f"{missing_year} dropped for a missing release_year"
    )


async def _probe_full_calls(model: str, runs: int, timeout: float, retries: int) -> None:
    print(f"=== 4. latency and contract at account scale, {runs} runs ===\n")
    titles = await _fetch_titles(ACCOUNT_SHOWS)
    if len(titles) < INTERESTED_CAP:
        print(
            f"only {len(titles)} shows in the local catalog — the payload would not be "
            "account-sized, so the latency figure would mean nothing. Refresh the "
            "catalog and re-run.\n"
        )
        return
    payload = _taste_payload(titles)
    rows = sum(len(payload[tier]) for tier in ("liked", "not_liked", "interested"))
    user = json.dumps(payload, separators=(",", ":"))
    print(f"payload: {rows} rows from real catalog titles, {len(user)} bytes of JSON\n")

    settings = get_settings()
    prompt = Prompt(system=_system_prompt(), user=user)
    latencies: list[float] = []
    # **Not `DEFAULT_RETRY_POLICY`.** Production's curve is four attempts of 60
    # seconds, so a model that cannot serve this payload costs four minutes per
    # run to learn nothing — which is what the first run of this probe did. The
    # measurement wants a ceiling high enough to see the real latency and few
    # enough attempts to fail fast; whether that latency fits under production's
    # 60 seconds is then a comparison, printed below, rather than the thing the
    # timeout silently decides.
    policy = RetryPolicy(max_retries=retries, timeout=timeout)
    async with OpenAICompatClient(
        provider=DEEPINFRA,
        api_key=settings.deepinfra_api_key,
        model=model,
        rate_calls=settings.deepinfra_rate_limit_requests,
        rate_window=settings.deepinfra_rate_limit_window_seconds,
        policy=policy,
    ) as client:
        for run in range(1, runs + 1):
            started = time.monotonic()
            try:
                response = await client.complete_json(prompt)
            except LLMError as exc:
                print(f"run {run}: {type(exc).__name__}: {exc}")
                continue
            elapsed = time.monotonic() - started
            latencies.append(elapsed)
            print(
                f"run {run}: {elapsed:6.2f}s  "
                f"in {response.input_tokens} / out {response.output_tokens} tokens  "
                f"{_contract_verdict(response.parsed)}"
            )

    if not latencies:
        print("\nno call succeeded — nothing to threshold against.\n")
        return
    median = statistics.median(latencies)
    print(f"\nlatency: min {min(latencies):.2f}s  median {median:.2f}s  max {max(latencies):.2f}s")
    print("\nsequential pass cost at the spec's threshold users (users x median latency):")
    for users in THRESHOLD_USERS:
        print(f"  {users:>4} users  {users * median / 60:6.1f} min")
    production = DEFAULT_RETRY_POLICY.timeout
    fits = "fits" if max(latencies) <= production else "does NOT fit"
    print(
        f"\nthe slowest run {fits} inside the client's production timeout "
        f"({production:.0f}s, `retry.DEFAULT_RETRY_POLICY`)."
    )
    print(
        "\nProject spec §10 puts the sequential-to-concurrent change at 100-200 users. "
        "The\nfigures above are what that claim should be re-read against.\n"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        action="append",
        help="measure this model id at account scale; repeat to compare ids. "
        "Catalog discovery runs either way — it is one request, and skipping it "
        "would mean the probe stops reading the catalog on exactly the machines "
        "where RECOMMENDATION_MODEL is already set.",
    )
    parser.add_argument("--runs", type=int, default=3, help="full-size calls to make (default 3)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="per-attempt ceiling for the full-size calls (default 180s, above "
        "production's 60s so a slow model is measured rather than timed out)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="retries per full-size call (default 1, below production's 3, so a "
        "model that cannot serve the payload fails fast)",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.deepinfra_api_key:
        print(
            "DEEPINFRA_API_KEY is not set. This probe measures the live API; there is "
            "nothing it can report without a key.",
            file=sys.stderr,
        )
        return 2

    async with httpx.AsyncClient(
        base_url=base_url_for(DEEPINFRA),
        headers={"Authorization": f"Bearer {settings.deepinfra_api_key}"},
        timeout=120.0,
    ) as raw:
        # The catalog is read on every run, whatever is configured: it is one
        # request, it is measurement 1, and an id that has been retired upstream
        # is exactly the thing a probe pinned to `RECOMMENDATION_MODEL` would
        # stop being able to tell you.
        catalog = await _list_models(raw)
        requested = list(
            args.model or ([settings.recommendation_model] if settings.recommendation_model else [])
        )
        working = await _confirm_models(raw, requested or catalog)
        if not working:
            print("no candidate answered a real call — stopping.", file=sys.stderr)
            return 1
        # §3 asks one question of one model; §4 measures each id under choice.
        measured = requested or working[:1]
        print(f"carrying {measured[0]} into §3, and {', '.join(measured)} into §4.\n")
        await _probe_json_mode(raw, measured[0])

    for model in measured:
        await _probe_full_calls(model, args.runs, args.timeout, args.retries)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
