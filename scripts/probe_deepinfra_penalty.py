"""Measure whether a frequency/presence penalty stops Flash cycling the same few titles.

NEU-1180's first diagnosis, and a *diagnosis* script rather than a shipped one: a
production run on 2026-08-18 came back `no_matches` having filled all 25
recommendation slots with **8 distinct titles**, every one of them a series
already named in its own input. Two failures at once — degeneracy (the same
handful repeated) and the exclusion ban ignored — and the first is the one a
sampling penalty can plausibly reach.

It replays **the exact payload that failed**, read from a file rather than
rebuilt, so the input is the one production actually sent and not a
reconstruction that happens to agree with it:

    docker compose exec tvbf-backend python scripts/probe_deepinfra_penalty.py \
        --payload /app/scratch/failed_payload.json --runs 2

Raw `httpx`, not `OpenAICompatClient`, for `probe_deepinfra.py`'s reason one
step further along: the client's wire body has no penalty fields at all, and
measuring what a penalty does is only possible below it. Raw calls spend no rate
budget — a couple of dozen sequential calls against a 5/s ceiling.

Scored on what the job actually keeps, not on what came back:

* **distinct** — how many of the returned titles are unique. 8 of 25 is the
  failure being chased.
* **already-had** — titles naming a series that appears anywhere in the payload
  (`liked`, `not_liked`, `interested`, `exclude`), folded and year-tolerant.
  These are exactly what `recommendations/exclusion.py` drops.
* **usable** — distinct *and* not already-had: the ceiling on what the pass
  could have stored. This is the number to compare across configurations, and
  the failing run scored zero.

It resolves nothing against `catalog.show`: a title the catalog lacks is a
different problem (`UNBELIEVABLE_UNRESOLVED_FRACTION`) and mixing the two would
make the score unreadable.

## What it found (2026-08-18): nothing, and that is the useful part

No configuration helped. Median usable across the runs went **2 at baseline to 1
under a penalty** — noise around zero, not a trend, and nowhere near the 12 a
full grid needs. Degeneracy is real in the output and a sampling penalty does
reach it, but the titles it stops repeating are replaced by other titles the
user already has, so the score the job cares about does not move.

So the ban being ignored is not a sampling artifact, and it is not fixable at
the request level. That is what sent NEU-1180 to
`scripts/probe_deepinfra_capacity.py` and, in the end, to a different model:
`DeepSeek-V4-Flash-0731` scores 0 usable at this payload size however it is
sampled. Keep this script — it is the cheap check that says a candidate's
non-compliance is the model rather than the knobs.
"""

import argparse
import asyncio
import json
import statistics
import sys
import unicodedata
from typing import Any

import httpx

from tvbf.config import get_settings
from tvbf.llm.registry import DEEPINFRA, base_url_for
from tvbf.recommendations.prompt import INSTRUCTION

# Each is one wire body's worth of sampling knobs. `None` means "do not send the
# field at all", which is the baseline: it reproduces today's request byte for
# byte, so a difference below is the penalty's and not the probe's.
CONFIGS: list[tuple[str, dict[str, Any]]] = [
    ("baseline (as shipped)", {}),
    ("frequency_penalty=0.3", {"frequency_penalty": 0.3}),
    ("freq=0.3 + presence=0.3", {"frequency_penalty": 0.3, "presence_penalty": 0.3}),
    ("frequency_penalty=0.6", {"frequency_penalty": 0.6}),
    ("frequency_penalty=1.0", {"frequency_penalty": 1.0}),
    ("presence_penalty=0.6", {"presence_penalty": 0.6}),
    ("freq=0.6 + presence=0.6", {"frequency_penalty": 0.6, "presence_penalty": 0.6}),
]


def fold(title: str) -> str:
    """Loose title equality, for counting only.

    Deliberately *not* `sql_fold.folded` — that one runs in Postgres and this
    script makes no database connection. It over-matches rather than under, so
    the already-had count errs toward reporting a problem.
    """
    stripped = unicodedata.normalize("NFKD", title.casefold())
    return "".join(c for c in stripped if c.isalnum())


def payload_titles(payload: dict[str, Any]) -> set[str]:
    """Every series named anywhere in the payload — the ban's own extent."""
    named: set[str] = set()
    for group in ("liked", "not_liked", "interested", "exclude"):
        for row in payload.get(group, []):
            named.add(fold(str(row[0])))
    return named


async def one_call(
    client: httpx.AsyncClient, model: str, key: str, user: str, extra: dict[str, Any]
) -> list[dict[str, Any]]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": INSTRUCTION},
            {"role": "user", "content": user},
        ],
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
        **extra,
    }
    response = await client.post(
        f"{base_url_for(DEEPINFRA)}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=body,
        timeout=120.0,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return json.loads(content).get("recommendations", [])


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True, help="the compiled payload json to replay")
    parser.add_argument("--runs", type=int, default=2, help="calls per configuration")
    parser.add_argument("--model", default=None, help="defaults to RECOMMENDATION_MODEL")
    parser.add_argument(
        "--only", default=None, help="run only configurations whose label contains this substring"
    )
    args = parser.parse_args()

    settings = get_settings()
    model = args.model or settings.recommendation_model
    key = settings.deepinfra_api_key
    if not model or not key:
        print("RECOMMENDATION_MODEL and DEEPINFRA_API_KEY must both be set", file=sys.stderr)
        return 1

    raw = open(args.payload).read().strip()
    payload = json.loads(raw)
    banned = payload_titles(payload)
    print(f"model:   {model}")
    print(f"payload: {len(raw)} bytes, {len(banned)} series named across every group")
    print(f"runs:    {args.runs} per configuration\n")

    header = (
        f"{'configuration':<24} {'returned':>8} {'distinct':>8}"
        f" {'already-had':>12} {'usable':>7}   (all runs)"
    )
    print(header)
    print("-" * len(header))

    async with httpx.AsyncClient() as client:
        for label, extra in CONFIGS:
            if args.only and args.only not in label:
                continue
            usables: list[int] = []
            distincts: list[int] = []
            hads: list[int] = []
            returned: list[int] = []
            for _ in range(args.runs):
                try:
                    entries = await one_call(client, model, key, raw, extra)
                except httpx.HTTPStatusError as exc:
                    print(f"{label:<24} HTTP {exc.response.status_code}: {exc.response.text[:120]}")
                    break
                titles = [fold(str(e.get("title", ""))) for e in entries if e.get("title")]
                unique = set(titles)
                had = unique & banned
                returned.append(len(entries))
                distincts.append(len(unique))
                hads.append(len(had))
                usables.append(len(unique - banned))
            if usables:
                print(
                    f"{label:<24} {statistics.median(returned):>8.0f} "
                    f"{statistics.median(distincts):>8.0f} {statistics.median(hads):>12.0f} "
                    f"{statistics.median(usables):>7.0f}   {sorted(usables)}"
                )

    print("\nusable = distinct and not already the user's — the ceiling on what the pass stores.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
