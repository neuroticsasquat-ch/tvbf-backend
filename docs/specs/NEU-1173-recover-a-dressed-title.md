# NEU-1173 — Recover the show title from a dressed LLM response

**Ticket:** [NEU-1173](https://linear.app/neuroticsasquatch/issue/NEU-1173/recommendations-recover-the-show-title-from-a-possessivequoted-llm)
**Repo:** `tvbf-backend` (backend only). Branches off `main` — `release/v0.3.1` no
longer exists on `origin`, and every recommendations change since NEU-1112 is on
`main`.
**Project spec:** [`tvbf-personalized-recommendations-project-spec.md`](tvbf-personalized-recommendations-project-spec.md) §7 (the output contract), §8 (resolution), §9.1 (the regeneration gate).

## Why

The model intermittently writes a comparison into `title` instead of the bare
series name. `recommendations/resolution.py` is fold-exact by design, so every
such recommendation matches nothing and is lost — and it is lost *invisibly*,
because the unresolved log line reads exactly like a catalog gap.

Measured on one production account across three prompt versions:

| prompt version | named | lost to a dressed title |
| -- | --: | --: |
| 1 (2026-08-17 14:27) | 25 | 20 |
| 3 (2026-08-17 16:32) | 25 | 4 |

`PROMPT_VERSION` 3 cut it from 80% to 16% and did not eliminate it. Three
consecutive revisions of `INSTRUCTION` have now tried — version 1 got prose,
version 2 got the user's own library handed back (`IGNORED_EXCLUSION_FRACTION`),
version 3 got this residue. **The durable fix is on the reading side**, because
the request half has been given three attempts and the failure keeps changing
shape rather than going away.

The four survivors, verbatim from that run's `raw_response`:

```
The Americans' sibling 'The Spy'
Halt and Catch Fire's 'The Company'
The Leftovers' 'Manhunt: Unabomber'
Killing Eve's 'Bodyguard'
```

One shape throughout: a series from the user's own payload, a possessive or
connective, then the actual recommendation in single quotes.

This is **not urgent**. The 25-asked / 12-displayed headroom (§7) absorbs four
losses without a user noticing. It is worth doing now because the headroom is
finite and because the losses are currently indistinguishable from genuine
catalog gaps in the only place we can see them.

## What the examples actually require

### The obvious rule fails all four cases

Every observed title has an **odd** number of apostrophes, because the connective
is a possessive:

```
The Americans' sibling 'The Spy'      ' after Americans │ ' before The │ ' after Spy
Halt and Catch Fire's 'The Company'   ' after Fire      │ ' before The │ ' after Company
The Leftovers' 'Manhunt: Unabomber'   ' after Leftovers │ ' before Man │ ' after Unabomber
Killing Eve's 'Bodyguard'             ' after Eve       │ ' before Body│ ' after Bodyguard
```

Pairing left to right — "the first quoted run", the ticket's own sketch —
recovers `" sibling "`, `"s "`, `" "` and `"s "`. Garbage in four cases out of
four. Pairing **from the right** recovers `The Spy`, `The Company`,
`Manhunt: Unabomber` and `Bodyguard`: correct in four cases out of four.

The version-1 example recorded in `prompt.INSTRUCTION`'s docstring is the fifth
witness and agrees:

```
Succession's corporate peer, 'Industry' (though you've seen it), try 'Billions'
```

The last run is `Billions`, which is the recommendation. The first is `Industry`,
which is the show being *declined*. Reading left to right does not merely fail
here — it recovers the one show the model was explicitly telling us the user
already has, which §8's exclusion filter would then have to drop.

That asymmetry is structural rather than lucky: in a dressed title the leading
segment is always the show being compared against or declined, and the trailing
quoted segment is always the recommendation.

### All four are reachable with the existing two tiers

Checked against the local catalog:

| candidate | resolves via | show |
| -- | -- | -- |
| `The Spy` | name | The Spy (2019) |
| `The Company` | name | The Company (2007) |
| `Bodyguard` | name | Bodyguard (2018) — three shows fold equal, `popularity` breaks it |
| `Manhunt: Unabomber` | **AKA** | Manhunt (2017) |

So AC 1 needs **no third resolution tier and no fuzzy matching**. `Manhunt:
Unabomber` is exactly the case NEU-1107 added the AKA tier for: TMDB names the
series *Manhunt* and carries `Manhunt: Unabomber` as an alternative title.

### The fold already strips the quote characters

`sql_fold.folded` removes all punctuation and whitespace, so `'Bodyguard'` and
`Bodyguard` fold identically. The extraction therefore has to decide **segment
boundaries and nothing else** — it never needs to trim quotes, and a candidate
that still carries stray punctuation is not thereby broken.

The same property is why a legitimately quoted title is unaffected: a show whose
real name contains quotes resolves as written on the first attempt, and the
fallback is never reached.

## What to build

### Where it lives

Three seams were considered.

**Rejected: `prompt.parse_suggestions`.** It would leave `Suggestion.title` clean
for everybody downstream, and that is the objection — the parser is a faithful
reading of §7, the model *violated* §7, and repairing it in the parser erases the
evidence that it did.

**Rejected: `recommendations/resolution.py`.** That module's contract is "two
tiers, in order, and nothing after that", and this is not a third tier. A dressed
title is not a harder catalog lookup; it is a response that did not honour the
output contract. Putting the repair there would also hand it to every future
caller of `resolve()`, which is wrong — no other caller is reading a model's
answer.

**Chosen: a pure helper in `recommendations/prompt.py`, called from
`_resolve_all`.** The knowledge *"this model dresses titles like this"* is
knowledge about the response half of the §7 contract, which is precisely what
`prompt.py` is and what its docstring says it is — it already records the shape
of every other violation this model commits. The decision to *act* on it stays in
the job, alongside the other two filters the job owns (exclusions, duplicates).
`resolution.py` is untouched and its docstring stays literally true.

It also puts the extraction where it can be tested without a database:
`tests/unit/recommendations/test_prompt.py` is DB-free, while every resolution
test is an integration test.

### The extraction

```python
def quoted_candidate(title: str) -> str | None:
```

Pure, no database, no fold.

- **Delimiter pairs:** `'…'`, `"…"`, `‘…’`, `“…”`.
- **Rule:** find the last delimiter character in the string; scan left for its
  partner; return what lies between, whitespace-stripped.
- Straight quotes partner with themselves. Typographic close-quotes partner with
  their open form — which pairs *more* reliably, since `’` is ambiguous between
  possessive and close-quote while `‘` unambiguously opens.
- Mixed delimiters fall out of the same-kind rule: `Killing Eve's "Bodyguard"`
  recovers `Bodyguard`.
- **`None`** when there is no delimiter, when the last delimiter has no partner
  to its left, when the span is empty or whitespace-only, or when the candidate
  equals the raw title (the raw already failed; an identical second query buys
  nothing).

Every failure mode is closed. An unpartnered delimiter — `The Americans' sibling
The Spy`, two apostrophes where the second is a close-quote that never opened —
yields no candidate at all rather than a guess.

**One candidate, not many.** Trying every run right-to-left until something
sticks would buy recall in principle and is the shape §8 explicitly refuses; each
extra candidate is another chance for a junk segment to land on a real show, and
`The Americans` *is* a real show. The last run is correct on all five observed
cases, and it is bounded at one extra query per unresolved title.

### The application

In `_resolve_all`, and only there:

- Fires **only** when `resolve(raw)` returned `None`. Never a preprocessing step
  (AC 2 is structural, not tested-into-existence).
- The candidate goes through the **same `resolve()`** with the **same
  `release_year`** — fold-exact on name, then AKA, year within ±1. §8's refusal
  of a fuzzy fallback is untouched, and the year is what makes a wrong extraction
  fail closed rather than confidently wrong.
- `Suggestion.title` keeps the raw form. The recovered candidate is a local.
- A recovered row passes through the exclusion and duplicate filters exactly as
  any other row does, in the same order (AC 5).

### Persistence

`app.user_recommendation` gains **`recovered_from: Text | None`** — the **raw
dressed title**, NULL on every ordinary row. One migration, one nullable column,
no constraint to keep in sync between Alembic and `create_all`.
`recommendation_repo.NewRecommendation` gains the field.

The raw title is stored rather than the candidate because the candidate is
re-derivable from it and the raw form is the thing that is otherwise lost.

**`matched_via` is deliberately not widened.** It answers "which catalog tier
matched" and its vocabulary stays `name` / `aka`. A recovered title still matched
via one of those; recovery is a property of the *title*, not of the tier, and
encoding two orthogonal facts in one column is the shape that goes stale. The
separate column keeps both retractable as a batch — `WHERE recovered_from IS NOT
NULL` — which is the stated reason `matched_via` exists at all.

`raw_response` already preserves every dressed title regardless. What the column
adds is the join from a *stored row* back to the dressed title that produced it,
so "did the fallback recover the right show?" is answerable by query a month
later, when the container logs have rotated. That is the whole justification for
this ticket applied to itself: a signal that lives only in rotating logs is the
weakest version of observability, and this repo has paid three times over for
"one bit could not say what happened" — `match_method`, `matched_via`, and the
three sync watermarks on `catalog.show`.

### Observability

- **One `INFO` per recovery**, at the point it fires: raw title → candidate →
  resolved show id. Mirrors how the exclusion and duplicate drops are already
  logged per item, and keeps the log and `recovered_from` saying the same thing.
- **A failed fallback annotates the unresolved line**: that title's entry becomes
  `The Leftovers' 'Some Show' (2019) [tried: Some Show]`.

  Without the annotation three different situations produce an identical line —
  no quotes present at all, quotes present but the candidate matched nothing,
  quotes present but the year disagreed. Those are a catalog gap, a wrong
  extraction rule, and the model sending the *leading* show's year respectively.
  Only the first is expected, and the log is the one window onto which is
  happening.
- **`_Attempt.recovered: int`** for the per-user summary line.

### The believability retry

`UNBELIEVABLE_UNRESOLVED_FRACTION = 0.5` exists *because of this incident* — its
docstring cites the 2026-08-17 run where the model wrote reasoning into `title`.
The version-1 run in this ticket (20 of 25 dressed) would have tripped it.

**Recovered titles count as resolved**, so `unresolved` collapses and that
threshold stops firing on this failure mode. That is intended: the retry exists
to obtain a *usable* answer, and recovery makes the answer usable, so there is
nothing left for a second call to buy.

**No new believability rule is added for recovery.** A "most titles needed
recovery, ask again" threshold is worse than it looks: `_ask` keeps whichever
attempt produced more rows, so a recovery-heavy first attempt will usually
produce *more* rows than a compliant retry of 25 — the rule would reliably spend
a call and then discard its result.

The observability that the quiet threshold gives up is what `recovered_from`
restores, by query rather than by tripwire. That pairing is the design.

### `PROMPT_VERSION` bumps to 4

`payload.PROMPT_VERSION` is in the regeneration hash (§9.1), and the rule as
written is that a change which "changes what the pass would produce while every
stored hash still matches" must bump it. This change does exactly that — same
payload, same instruction, different stored rows.

`INSTRUCTION`'s text is **not** changing, so the bump is a small dishonesty about
what the constant names. The alternative is worse: without it, a user whose taste
has not moved keeps last week's set — the one missing the four recovered shows —
until something in their history changes, and the fix is never exercised against
real users, which is the only way to evaluate it. Amend the constant's docstring
to say it versions the whole request/response contract, of which the reading side
is half.

Cost: 3–5 calls on the Sunday after deploy.

### Docstrings amended in the same commit

This repo treats docstrings as the record, so three go stale on merge:

- `UNBELIEVABLE_UNRESOLVED_FRACTION` — its motivating incident is now handled a
  layer earlier. Leaving it claiming otherwise makes the constant read as
  load-bearing for a case it no longer sees.
- `PROMPT_VERSION` — as above.
- `resolution.py`'s "two tiers, and nothing after that" — still true, and should
  point at where the repair lives so the next reader does not add a third tier
  there.

## Acceptance criteria

1. Each of the four verbatim titles resolves to the show named inside the quotes,
   given the year the model sent — `The Spy`, `The Company`, `Manhunt` (via its
   `Manhunt: Unabomber` AKA), `Bodyguard`.
2. A title that resolves as written is unaffected; the fallback never runs. Proven
   by a seeded case where the raw title and the extractable segment name
   *different* shows and the raw one wins.
3. A quoted segment that resolves to nothing, or to a show whose year disagrees,
   still yields `None`, is still counted unresolved, and still appears in the
   "resolved to nothing" log — now carrying `[tried: …]`.
4. A recovered row is logged as recovered with the raw form, and carries that raw
   form in `recovered_from`.
5. The exclusion and duplicate filters apply to a recovered show exactly as to any
   other.
6. `PROMPT_VERSION` is 4, so every user regenerates once on the next pass.

## Tests

**Unit** (`tests/unit/recommendations/test_prompt.py`, no database):

- The four verbatim titles and the version-1 `Succession's … 'Billions'` case
  extract correctly. These are the regression suite — they are real model output,
  not authored examples, which is the same argument
  `tests/fixtures/recommendations/` rests on.
- Left-to-right pairing is wrong: assert `The Americans' sibling 'The Spy'` yields
  `The Spy` and never `" sibling "`.
- Delimiter coverage: straight single, straight double, both typographic pairs,
  and mixed (`Eve's "Bodyguard"`).
- Negatives: no delimiter; unpartnered trailing delimiter; empty span; whitespace
  span; candidate equal to the raw title.

**Integration** (`tests/integration/recommendations/`, seeding its own catalog
rows per the fixture rule):

- AC 1 — all four, including the AKA-tier one.
- AC 2 — raw wins over an extractable segment naming a different seeded show.
- AC 3 — candidate resolving to nothing, and candidate resolving to a show whose
  year is outside ±1, both yield `None`.
- AC 4 — `recovered_from` holds the raw title on a recovered row and is NULL on an
  ordinary one.
- AC 5 — a recovered show that is in `excluded_show_ids` is dropped; a recovered
  show already named by an earlier suggestion is counted a duplicate.

## Out of scope

- **Any change to `INSTRUCTION`'s wording.** This ticket is the reading-side fix
  precisely because three revisions of the writing side have not held.
- **Any fuzzy, trigram or similarity fallback.** §8's refusal stands. The two
  trigram indexes this path reads with `=` are still not an invitation to read
  them with `%`.
- **Recovering a title that carries no quotes at all** — `The Americans' sibling
  The Spy`. There is no delimiter to key on and any rule for it would be the
  fuzzy matching above under another name.
- **Serving `recovered_from` on `GET /me/recommendations`.** It is a diagnostic,
  like `matched_via`, which that contract also does not carry.

## Notes for whoever implements this

- `release/v0.3.1` is gone from `origin`; branch off `main`.
- The first pass after deploy is where the extraction rule meets live answers,
  because `PROMPT_VERSION` 4 re-runs every user. Read that run's log before
  assuming it holds.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
