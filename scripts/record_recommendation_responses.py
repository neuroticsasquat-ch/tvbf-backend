"""Record real DeepSeek-on-DeepInfra responses as committed test fixtures.

The recommendations project spec §12 asks for **recorded real responses, at
least three: one clean, one carrying a title that will not resolve, one
malformed.** The reason is the one that makes fixtures worth committing at all:
a hand-written fixture encodes what we *think* the model returns, and the failure
modes that matter are the ones nobody would think to write down. So these are
recordings, not inventions — `tests/fixtures/recommendations/` holds the exact
response bodies DeepInfra sent, and `README.md` there records what each one is.

Run inside the container with `DEEPINFRA_API_KEY` set in `.env`:

    docker compose exec tvbf-backend python scripts/record_recommendation_responses.py \
        --model ByteDance/Seed-2.0-mini

**Pass `--model` unless your `.env` already holds the id `.env.example` records.**
It defaults to `RECOMMENDATION_MODEL`, which is a local setting and drifts: a
recording taken from a model the weekly pass will not call pins the output shape
of the wrong thing, and nothing downstream can tell. The recorded envelope
carries the id that answered, and this script prints it — check it.

It writes one file per `--case` and prints each recorded title with the year the
model gave it. Costs a handful of calls — cents.

Three deliberate properties, each of which a tidier script would lose:

**Raw `httpx`, not `OpenAICompatClient`.** The malformed case is recorded from a
reasoning model whose answer the client rejects outright (`LLMResponseInvalid`,
measured in NEU-1100: `DeepSeek-R1-0528` puts a `<think>` block ahead of its
JSON), and a recorder that cannot record the responses we most want on disk is
not a recorder. This also spends no rate budget, which is safe at this volume for
the reason `probe_deepinfra.py` says it is.

**The whole response envelope is written, not just the message content.** `usage`
is what M4 stores per run and `model` is what says which id answered; a fixture
trimmed to the content is one that cannot exercise either.

**The instruction here is not M4's.** The prompt the weekly pass sends is
NEU-1109's to write, and it will differ in wording. What these fixtures pin is
what a real model does with the §7 output contract — the shape of what comes
back, and the ways it comes back wrong — not the exact bytes of a prompt that
does not exist yet. Re-record if the contract itself changes.
"""

import argparse
import asyncio
import json
import pathlib
import sys

import httpx

from tvbf.config import get_settings
from tvbf.llm.registry import DEEPINFRA, base_url_for

FIXTURE_DIR = pathlib.Path(__file__).resolve().parents[1] / "tests/fixtures/recommendations"

# The §7 output contract, stated the way the weekly pass will have to state it,
# and carrying the literal lower-case word "json" that `client._to_wire`
# enforces.
INSTRUCTION = (
    "You recommend television series. Reply with a json object of the form "
    '{"recommendations": [{"title": "...", "release_year": 1234, "reason": "one sentence"}]} '
    "and nothing else. Give exactly 25 recommendations, the count §7 asks the weekly pass for. "
    "`title` is the series title as it is "
    "best known in English, `release_year` is the year the series first aired, and `reason` "
    "is one plain sentence. Never recommend a series that appears in the input."
)

# Taste payloads in the compiled columnar shape (`recommendations/payload.py`),
# with real titles and real years. A model asked about invented shows answers a
# different question, and what is being recorded here is the real answer.
CASES: dict[str, str] = {
    "clean": json.dumps(
        {
            "columns": ["title", "year", "pct", "stars"],
            "liked": [
                ["Breaking Bad", 2008, 100, 5.0],
                ["The Sopranos", 1999, 100, 5.0],
                ["Better Call Saul", 2015, 100, 4.5],
                ["Mad Men", 2007, 88, 4.5],
                ["The Wire", 2002, 100, 5.0],
            ],
            "not_liked": [["Emily in Paris", 2020, 12, 1.5]],
            "interested": [["Deadwood", 2004, 0, None]],
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ),
    # Deliberately obscure and international, which is where a model is most
    # likely to name something the mirror has under another title or not at all.
    "obscure": json.dumps(
        {
            "columns": ["title", "year", "pct", "stars"],
            "liked": [
                ["La casa de papel", 2017, 100, 4.5],
                ["Dark", 2017, 100, 5.0],
                ["Gomorra", 2014, 74, 4.5],
                ["Les Revenants", 2012, 100, 4.0],
                ["Kaos", 2024, 40, 3.5],
            ],
            "not_liked": [],
            "interested": [["Babylon Berlin", 2017, 0, None]],
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ),
}


async def _record(
    raw: httpx.AsyncClient, *, model: str, user: str, path: pathlib.Path
) -> dict | None:
    response = await raw.post(
        "/chat/completions",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": INSTRUCTION},
                {"role": "user", "content": user},
            ],
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        },
    )
    if response.status_code != 200:
        print(f"  ! {model} answered {response.status_code}: {response.text[:200]}")
        return None
    body = response.json()
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  wrote {path.relative_to(FIXTURE_DIR.parents[2])}")
    return body


def _report(body: dict) -> None:
    """Print what the recorded response says, so the file can be named for it."""
    content = body["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        print(f"  content does not decode as JSON ({exc}); first 120 chars: {content[:120]!r}")
        return
    for entry in parsed.get("recommendations", []):
        print(f"    {entry.get('title')!r} ({entry.get('release_year')})")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", action="append", choices=sorted(CASES), help="repeatable; default all"
    )
    parser.add_argument("--model", help="default: RECOMMENDATION_MODEL")
    parser.add_argument(
        "--reasoning-model",
        default="deepseek-ai/DeepSeek-R1-0528",
        help="the model recorded for the malformed case; pass '' to skip it",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.deepinfra_api_key:
        print("DEEPINFRA_API_KEY is not set", file=sys.stderr)
        return 1
    model = args.model or settings.recommendation_model
    if not model:
        print("no model: pass --model or set RECOMMENDATION_MODEL", file=sys.stderr)
        return 1

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        base_url=base_url_for(DEEPINFRA),
        headers={"Authorization": f"Bearer {settings.deepinfra_api_key}"},
        timeout=180.0,
    ) as raw:
        for case in args.case or sorted(CASES):
            print(f"{case} ({model}):")
            body = await _record(
                raw, model=model, user=CASES[case], path=FIXTURE_DIR / f"{case}.json"
            )
            if body is not None:
                _report(body)
        if args.reasoning_model:
            print(f"malformed ({args.reasoning_model}):")
            body = await _record(
                raw,
                model=args.reasoning_model,
                user=CASES["clean"],
                path=FIXTURE_DIR / "malformed.json",
            )
            if body is not None:
                _report(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
