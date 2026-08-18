"""Which DeepInfra models still obey the exclusion rule as a user's library grows?

The weekly recommendations pass degrades with library size, and it degrades
*silently*: a run that recommends only series the user already has is filtered to
nothing and recorded `no_matches`, which looks like a quiet week rather than a
failure. Measured on production 2026-08-18, and dose-dependent across accounts:
series named in the payload → rows stored, 21 → 25, 95 → 21, **261 → 0** and
**494 → 3**. The two failing accounts are the point. The 261-series one stored
nothing at all; the 494-series one had 23 of its 25 returned titles already in
its own payload, which is the same defect one step further along.

So the question this answers is not "which model is best" but **"which models
still work at the library size this user will have in two years, and what does
that cost per user per year"**.

## Why this can be screened automatically

The metric needs no ground truth and no human judgement: **any title the model
returns that already appears in its own input is a violation**, at any payload
size, for any user, real or synthetic. `usable` is the count of returned titles
that are distinct *and* absent from the payload — the ceiling on what the pass
could store, where 12 is a full grid. That property is what makes a 100-model
sweep possible at all.

It deliberately does **not** score recommendation quality, which cannot be
automated and should not be faked: a model can obey perfectly and recommend
rubbish. Compliance is the necessary half; run the finalists against a real
payload and read the output for the other half.

## The cascade

Screening the whole catalog at every size would cost more than it is worth, and
most models fail immediately, so cost is spent in proportion to how far a model
gets:

* **Round A** — every candidate, `--round-a-runs` calls at `ROUND_A_SIZE`,
  one step above the library as it stands today.
  A deliberately low bar: parseable json, and at least one usable suggestion in
  any run. This is what eliminates the models that emit a `<think>` block ahead
  of their json (`DeepSeek-R1-0528` does, measured NEU-1100) or ignore
  `response_format` — behaviourally, because the catalog's `reasoning` tag does
  not predict it: `DeepSeek-V4-Flash-0731` carries that tag and answers cleanly.
* **Round B** — the top `--finalists` by round A, at every size, `--round-b-runs`
  each. This is the capacity curve, and the size at which a model's usable count
  collapses is its ceiling. Pick on **headroom**, not on passing today.

## Payloads are grown from a real one, never synthesised

`--payload` is a real `app.user_recommendation_set.compiled_payload`; the larger
sizes pad its `liked` group with real catalog titles from `--padding`. A bag of
invented shows would measure something else entirely — the payload works by
naming series the model already knows, so the titles *are* the taste signal, and
synthetic ones would test recall of things that do not exist. Padding with real
shows keeps the taste coherent while the ban grows, which is the actual
question: this library in two years.

## Two departures, both `probe_deepinfra.py`'s and both wider here

**Raw `httpx`, not `OpenAICompatClient`.** The client refuses to send a prompt
without the word "json" and reads one provider's model at a time; a sweep needs
`/models`, which is not on its call surface at all, and needs to vary the model
per call.

**No rate budget is spent.** `probe_deepinfra.py` justifies that on volume — "a
few dozen sequential calls against a 5/s ceiling" — and this script is far past
a few dozen, so the justification has to be `--concurrency` instead: at most 4
requests are ever in flight, each of which takes seconds at these payload sizes,
so the arrival rate stays an order of magnitude under the 5/s
`DEEPINFRA_RATE_LIMIT_REQUESTS` ceiling without consulting the bucket. **Do not
raise `--concurrency` into double figures without pointing this at the real
limiter** — the budget is cross-process and shared with the weekly pass
(ADR-0006), and a sweep that outruns it steals from a job that fails closed.

## Spending

`--budget` is a hard ceiling in dollars, checked before every call against
catalog pricing and the tokens the provider reports. The sweep stops rather than
overruns. Results stream to `--out` as jsonl, so a stop — budget, error or
Ctrl-C — keeps everything already measured.

    docker compose exec tvbf-backend python scripts/probe_deepinfra_capacity.py \
        --payload /tmp/failed_payload.json --padding /tmp/padding_shows.json \
        --budget 4.00 --out /tmp/capacity.jsonl

## What it found

**Deliberately not recorded here.** The results — the finalists' curves, their
cost per user per year, and the id production runs — live in the umbrella
`docs/recommendations-model-measurements.md`, which is not a git repo. This
repository is public and the running model id is the one part of this subsystem
worth keeping to ourselves.

Two findings are worth carrying in the open, because they are about *method*
rather than about any model, and re-running this script without them wastes the
spend:

**The best-complying model gave the worst answers.** A model that topped the
curve returned coherent-but-generic prestige-adjacent titles for a profile that
wanted something specific, while a lower scorer on the same payload matched the
account's known-good set from the day before. This sweep screens out models that
cannot obey the exclusion rule; it does not pick among the ones that can. **Any
model change needs both stages — this curve, then a human reading one real
payload's output.**

**This sweep cannot screen latency and will hand you a model the client refuses
to wait for.** It scores at `timeout=240`, far above `retry.DEFAULT_RETRY_POLICY`.
An id has been chosen on this curve, never timed, and set — and both production
accounts came back `failed` with four timed-out attempts apiece. Run
`scripts/probe_deepinfra.py --model <id>` at account scale before setting
anything.

"""

import argparse
import asyncio
import json
import statistics
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import httpx

from tvbf.config import get_settings
from tvbf.jobs.weekly_recommendations import BYTES_PER_TOKEN
from tvbf.llm.registry import DEEPINFRA, base_url_for
from tvbf.recommendations.prompt import INSTRUCTION

MODELS_URL = "https://api.deepinfra.com/v1/openai/models"

# Sizes are *total series named in the payload*, which is what the model has to
# hold. The smallest is the production payload as it stands today.
SIZES = (260, 500, 1000, 2000)
ROUND_A_SIZE = 500

# Named explicitly so a cheap model that is plausible for this job cannot be
# dropped by a price ceiling or a catalog rotation. Each is cheap for its class:
# a sparse mixture with a small active set, which is what actually predicts
# price here — not recency. (Meta-Llama-3.1-70B costs 4x the newer Llama-3.3-70B;
# Qwen2.5-72B costs 4x the newer, larger Qwen3-235B-A22B.)
PINNED = (
    "deepseek-ai/DeepSeek-V4-Flash-0731",
    "deepseek-ai/DeepSeek-V3.2",
    "Qwen/Qwen3-235B-A22B-Instruct-2507",
    "meta-llama/Llama-4-Scout-17B-16E-Instruct",
    "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "google/gemini-3.1-flash-lite",
    "zai-org/GLM-4.7",
    "deepseek-ai/DeepSeek-V4-Pro-0813",
)


@dataclass
class Model:
    id: str
    price_in: float
    price_out: float
    context: int

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000


@dataclass
class Spend:
    """The running bill, and the only thing allowed to stop the sweep.

    Cost is **reserved before the call and settled after it**, because the sweep
    runs several calls at once: a check that only reads `spent` is passed by
    every in-flight call before any of them has recorded anything, and the
    budget is exceeded by up to one full batch. Reserving bounds the overrun to
    the difference between the estimate and the truth.
    """

    budget: float
    spent: float = 0.0
    reserved: float = 0.0
    calls: int = 0

    def reserve(self, estimate: float) -> bool:
        """Take `estimate` from the budget, or refuse if it does not fit."""
        if self.spent + self.reserved + estimate > self.budget:
            return False
        self.reserved += estimate
        return True

    def settle(self, estimate: float, actual: float) -> None:
        self.reserved -= estimate
        self.spent += actual
        self.calls += 1

    @property
    def remaining(self) -> float:
        return self.budget - self.spent - self.reserved


@dataclass
class Result:
    model: str
    size: int
    usable: list[int] = field(default_factory=list)
    already_had: list[int] = field(default_factory=list)
    unparseable: int = 0
    errors: list[str] = field(default_factory=list)
    cost: float = 0.0
    # A cell nobody paid for is not a cell that failed. Without this the report
    # reads a budget stop as a catalog of broken models.
    skipped: bool = False

    @property
    def median_usable(self) -> float:
        return statistics.median(self.usable) if self.usable else 0.0


def fold(title: str) -> str:
    """Loose title equality, for counting only — this script opens no database.

    Deliberately *not* `sql_fold.folded`, which is the one fold and runs in
    Postgres. The rule it belongs to governs titles compared on a read or a
    write path; nothing here reaches the catalog, and scoring a sweep must not
    need a database connection at all.

    Over-matches rather than under, so the already-had count errs toward
    reporting a problem rather than hiding one.
    """
    stripped = unicodedata.normalize("NFKD", str(title).casefold())
    return "".join(c for c in stripped if c.isalnum())


def grow(payload: dict[str, Any], padding: list[list[Any]], target: int) -> dict[str, Any]:
    """The payload as it would look at `target` series named.

    Padding lands in `liked` carrying a plausible rating, because that is the
    group that grows without bound in life — `interested` is already capped at 50
    and `exclude` only names what no tier group shows.
    """
    grown = {k: (list(v) if isinstance(v, list) else v) for k, v in payload.items()}
    named = sum(len(grown.get(g, [])) for g in ("liked", "not_liked", "interested", "exclude"))
    for i, (title, year) in enumerate(padding):
        if named >= target:
            break
        # Ratings cycle 3-5: padding is meant to read as more of the same taste,
        # not as a second opinion the model has to reconcile.
        grown["liked"].append([title, year, 100, [3, 4, 5][i % 3]])
        named += 1
    return grown


def named_in(payload: dict[str, Any]) -> set[str]:
    return {
        fold(row[0])
        for group in ("liked", "not_liked", "interested", "exclude")
        for row in payload.get(group, [])
    }


async def fetch_catalog(client: httpx.AsyncClient, key: str, ceiling: float) -> list[Model]:
    response = await client.get(MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=60)
    response.raise_for_status()
    models: list[Model] = []
    for row in response.json().get("data", []):
        meta = row.get("metadata") or {}
        if "chat" not in (meta.get("tags") or []):
            continue
        pricing = meta.get("pricing") or {}
        price_in, price_out = pricing.get("input_tokens"), pricing.get("output_tokens")
        if price_in is None or price_out is None:
            continue
        if price_in > ceiling and row["id"] not in PINNED:
            continue
        models.append(Model(row["id"], price_in, price_out, meta.get("context_length") or 0))
    return models


async def one_call(
    client: httpx.AsyncClient, key: str, model: Model, user: str
) -> tuple[list[dict[str, Any]] | None, int, int]:
    """One request. Returns (entries or None if unparseable, tokens_in, tokens_out)."""
    response = await client.post(
        f"{base_url_for(DEEPINFRA)}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model.id,
            "messages": [
                {"role": "system", "content": INSTRUCTION},
                {"role": "user", "content": user},
            ],
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        },
        timeout=240.0,
    )
    response.raise_for_status()
    body = response.json()
    usage = body.get("usage") or {}
    tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0)
    choices = body.get("choices") or []
    content = choices[0].get("message", {}).get("content") if choices else None
    # Measured: some models answer 200 with a null `content` — the whole reply
    # went into a reasoning field, or it declined. Unparseable is the honest
    # reading, and it must not be an exception: one such model would otherwise
    # take down the gather that every other model's result is riding on.
    if not isinstance(content, str):
        return None, tokens_in, tokens_out
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # A `<think>` block ahead of the json, or a truncated answer. The job
        # would raise LLMResponseInvalid here; for screening it is a failure.
        return None, tokens_in, tokens_out
    if not isinstance(parsed, dict):
        return None, tokens_in, tokens_out
    return parsed.get("recommendations") or [], tokens_in, tokens_out


async def measure(
    client: httpx.AsyncClient,
    key: str,
    model: Model,
    user: str,
    banned: set[str],
    size: int,
    runs: int,
    spend: Spend,
) -> Result:
    result = Result(model=model.id, size=size)
    # `BYTES_PER_TOKEN` is imported rather than re-typed: it is a measurement
    # (NEU-1100) that lives in one place, and a copy here would drift silently
    # against the job whose spend this estimates. It errs high on this payload
    # shape, so the reservation errs toward under-spending rather than over.
    estimate = model.cost(round(len(user.encode("utf-8")) / BYTES_PER_TOKEN), 1100)
    for _ in range(runs):
        if not spend.reserve(estimate):
            result.skipped = not result.usable and not result.errors
            break
        try:
            entries, tokens_in, tokens_out = await one_call(client, key, model, user)
        except httpx.HTTPStatusError as exc:
            spend.settle(estimate, 0.0)
            result.errors.append(f"HTTP {exc.response.status_code}")
            break
        except httpx.HTTPError as exc:
            spend.settle(estimate, 0.0)
            result.errors.append(type(exc).__name__)
            break
        cost = model.cost(tokens_in, tokens_out)
        spend.settle(estimate, cost)
        result.cost += cost
        if entries is None:
            result.unparseable += 1
            continue
        titles = {fold(e.get("title", "")) for e in entries if e.get("title")}
        result.usable.append(len(titles - banned))
        result.already_had.append(len(titles & banned))
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True, help="a real compiled_payload json")
    parser.add_argument("--padding", required=True, help="json array of [title, year] to grow with")
    parser.add_argument("--budget", type=float, default=4.0, help="hard ceiling, dollars")
    parser.add_argument("--price-ceiling", type=float, default=5.0, help="max $/1M input tokens")
    parser.add_argument("--round-a-runs", type=int, default=2)
    parser.add_argument("--round-b-runs", type=int, default=3)
    parser.add_argument("--finalists", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", default="/tmp/capacity.jsonl")
    parser.add_argument(
        "--models",
        default=None,
        help="comma-separated ids to screen instead of the catalog — for finishing a curve "
        "that a budget stop left half-measured, without paying for round A again",
    )
    args = parser.parse_args()

    settings = get_settings()
    key = settings.deepinfra_api_key
    if not key:
        print("DEEPINFRA_API_KEY must be set", file=sys.stderr)
        return 1

    payload = json.loads(open(args.payload).read().strip())
    padding = json.loads(open(args.padding).read().strip())
    grown = {size: grow(payload, padding, size) for size in SIZES}
    bodies = {size: json.dumps(p, separators=(",", ":")) for size, p in grown.items()}
    bans = {size: named_in(p) for size, p in grown.items()}
    for size in SIZES:
        print(f"  size {size:>5}: {len(bans[size]):>5} named, {len(bodies[size]):>7} bytes")

    spend = Spend(args.budget)
    out = open(args.out, "w")
    semaphore = asyncio.Semaphore(args.concurrency)

    def emit(result: Result, phase: str) -> None:
        out.write(json.dumps({"phase": phase, **result.__dict__}) + "\n")
        out.flush()

    async with httpx.AsyncClient() as client:
        models = await fetch_catalog(client, key, args.price_ceiling)
        if args.models:
            wanted = {m.strip() for m in args.models.split(",")}
            models = [m for m in models if m.id in wanted]
            missing = wanted - {m.id for m in models}
            if missing:
                print(f"not in catalog (or over the ceiling): {', '.join(sorted(missing))}")
        print(f"\ncandidates: {len(models)} chat models at or under ${args.price_ceiling}/1M input")
        print(f"budget:     ${args.budget:.2f}\n")

        async def guarded(model: Model, size: int, runs: int) -> Result:
            """One model's measurement, isolated.

            A sweep is only worth running if a single misbehaving model costs
            its own row and nothing else — `asyncio.gather` propagates the first
            exception and discards every sibling result, which on a paid sweep
            means throwing away work already bought.
            """
            async with semaphore:
                try:
                    return await measure(
                        client, key, model, bodies[size], bans[size], size, runs, spend
                    )
                except Exception as exc:  # noqa: BLE001 - one row must not end the sweep
                    return Result(model=model.id, size=size, errors=[type(exc).__name__])

        async def run_and_emit(model: Model, size: int, runs: int, phase: str) -> Result:
            """Measure one cell and write it out *as it finishes*.

            Emitting after the round's `gather` instead loses every call already
            paid for when the sweep is stopped — which is the one thing a paid
            sweep must not do, and exactly what happened on 2026-08-18.
            """
            result = await guarded(model, size, runs)
            emit(result, phase)
            return result

        print(f"--- round A: all candidates at {ROUND_A_SIZE} series ---")
        round_a = await asyncio.gather(
            *(run_and_emit(m, ROUND_A_SIZE, args.round_a_runs, "A") for m in models)
        )
        by_id = {m.id: m for m in models}
        survivors = [r for r in round_a if r.usable and max(r.usable) > 0]
        skipped = sum(1 for r in round_a if r.skipped)
        print(f"parseable and non-zero: {len(survivors)} of {len(models) - skipped} measured")
        if skipped:
            print(f"NOT MEASURED (budget): {skipped} models — raise --budget to cover them")
        print(f"spent so far: ${spend.spent:.2f}\n")

        finalists = sorted(survivors, key=lambda r: -r.median_usable)[: args.finalists]
        print(f"--- round B: top {len(finalists)} across {SIZES} ---")
        curves = await asyncio.gather(
            *(
                run_and_emit(by_id[r.model], size, args.round_b_runs, "B")
                for r in finalists
                for size in SIZES
            )
        )

    out.close()

    print(f"\n{'model':<46} " + " ".join(f"{s:>6}" for s in SIZES) + f" {'$/user/yr':>10}")
    print("-" * (46 + 7 * len(SIZES) + 11))
    grid: dict[str, dict[int, Result]] = {}
    for result in curves:
        grid.setdefault(result.model, {})[result.size] = result
    for model_id, row in sorted(
        grid.items(), key=lambda kv: -(kv[1].get(SIZES[-1]) or Result("", 0)).median_usable
    ):
        model = by_id[model_id]
        cells = []
        for size in SIZES:
            r = row.get(size)
            if r is None:
                cells.append("   n/a")
            elif r.usable:
                cells.append(f"{r.median_usable:>6.0f}")
            elif r.skipped:
                cells.append("  skip")
            else:
                cells.append("   err")
            cells[-1] = f"{cells[-1]:>6}"
        # Weekly, at the smallest payload — what running this model would cost.
        per_year = model.cost(3491, 1044) * 52
        print(f"{model_id:<46} " + " ".join(cells) + f" {per_year:>10.2f}")

    print(f"\n{spend.calls} calls, ${spend.spent:.2f} spent of ${args.budget:.2f}")
    print(f"per-model detail in {args.out}")
    print("\nusable = returned titles absent from the payload; 12 is a full grid.")
    print("Compliance only — read a finalist's real output before choosing on it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
