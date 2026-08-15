# Recorded model responses

Real responses from DeepSeek on DeepInfra, recorded by
`scripts/record_recommendation_responses.py` on **2026-08-15** and committed
verbatim — the whole chat-completions envelope, `usage` and all.

The project spec (§12) asks for recordings rather than hand-written fixtures
because a hand-written fixture encodes what we *think* the model returns, and the
failure modes that matter are the ones nobody would think to write down. The
`malformed` file is exactly that: nothing here would have invented it.

| File | Model | What it is |
| --- | --- | --- |
| `clean.json` | `deepseek-ai/DeepSeek-V4-Pro-0813` | Twenty-five recommendations, every one of which resolves — 23 by name and two (`Gomorrah`, `The Bureau`) through the AKA tier. |
| `obscure.json` | `deepseek-ai/DeepSeek-V4-Pro-0813` | Twenty-five from an international taste profile. One (`Twin Peaks: The Return`, 2017) resolves to **nothing** — a real catalog gap rather than a hallucination, since the mirror carries *Twin Peaks* (1990) and models the 2017 revival as a season of it. Four more (`Trapped` → *Ófærð*, `Deadwind` → *Karppi*, `The Chalet` → *Le Chalet*, `Suburra: Blood on Rome` → *Suburra: La Serie*) resolve only through the AKA tier, which is why that tier exists. |
| `malformed.json` | `deepseek-ai/DeepSeek-R1-0528` | A reasoning model's answer: a `<think>` block ahead of the JSON, so the content does not decode. HTTP 200, `finish_reason: "stop"`, JSON mode requested and honoured as far as the provider is concerned. |

Twenty-five because that is what §7 asks the weekly pass for; the model id is the
one NEU-1100 measured and `.env.example` sets, not whatever a local `.env`
happens to hold — a fixture recorded from a model the pass will not call pins the
output shape of the wrong thing. The reasoning model is deliberately a different
id: it is the failure being recorded.

Two things to know before touching them.

**The instruction that produced these is the recorder's, not the weekly pass's.**
The real prompt is NEU-1109's to write. What these pin is what a real model does
with the §7 output contract — the shape of what comes back, and the ways it comes
back wrong — not the bytes of a prompt that does not exist yet. Re-record if the
contract changes; there is no need to re-record when the prompt's wording does.

**The titles were checked against the real local catalog**, not against the rows
the tests seed. That is what makes "resolves" and "resolves to nothing" above
claims about the recording rather than about a fixture built to agree with it.
The tests seed their own catalog anyway (spec §12's known constraint: `catalog`
is sparsely populated locally while the ingest runs).
