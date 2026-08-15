# Recommendations are generated from watch behaviour

**Status:** accepted (2026-08-15)
**Context:** [ADR-0002](./0002-no-upstream-api-in-request-path.md), [ADR-0008](./0008-user-data-references-internal-ids.md), NEU-1101, and the *TVBF: Personalized Recommendations* project spec (`docs/specs/tvbf-personalized-recommendations-project-spec.md`, umbrella-relative)

"Recommended for you" is produced by asking a language model what a user should watch next,
given a compiled description of what that user has already watched, and then resolving the
model's answers to rows in `catalog.show`. It is not produced by comparing users to each
other, it does not call anything upstream while a request is in flight, and it is not cached.

Three decisions are recorded here. They are separable but they hold each other up: the first
is why there is a model call at all, the second is what the call is allowed to cost, and the
third is where the call is allowed to happen.

**Numbering note.** The project spec calls this ADR-0009 — the next free number when that spec
was drafted. The TMDB migration then landed four decisions of its own (0009 through 0012) while
this project sat behind the cutover. This is that ADR, under the number that was actually
free.

## 1. This is an LLM generation task, not collaborative filtering

TMDB Discovery's spec hands "people who watched this also watched" to this project. That
signal is not buildable here, and the obstacle is not implementation effort.

**Production has 5 users.** A co-watching matrix over five people and 563 distinct shows is
not a weak signal that better weighting would sharpen — it is noise, and no amount of tuning
fixes a sample-size problem. Every knob a recommender exposes (neighbourhood size, shrinkage,
implicit-feedback confidence) is a way of trading bias against variance in a sample; at n=5
there is nothing on either side of that trade.

What the feature actually needs is a bridge from *what this person watched* to *what exists
that they have not seen*. That is world knowledge about television, and **it is not a
statistic we hold** — it is not in `app`, and it is not in `catalog` either, which knows what
shows exist but nothing about which of them scratch the same itch. A language model is the
only component available that has it.

So the shape is **generation with a resolution step**, not ranking over a candidate set. Two
consequences follow and both are load-bearing:

- **No other user's data enters a prompt, ever.** Feeding user B's history into a request
  that produces recommendations for user A leaks B's viewing to a third party in exchange for
  a correlation computed over four other people. At this scale that trade is not close.
  Friend-derived signals are out of scope for the same reason, not merely for scope's sake.
- **The model's taste *is* the product.** There is no ranking layer, no feedback loop, and no
  post-processing that rescues a weak suggestion. Everything downstream of the response is
  resolution and filtering — which is why the quality lever is the payload and the prompt,
  and why `compiled_payload` and `raw_response` are stored per run.

**What would reverse this.** Not a better algorithm — a bigger sample. Collaborative
filtering becomes worth revisiting when the user base is large enough that co-watching is a
measurement rather than a coincidence, which is orders of magnitude away from five.

## 2. The provider is chosen for per-user cost scaling, not for model tier

The workload is **one call per changed user per week**. Cost is therefore linear in accounts,
and the thing that has to stay true as accounts are added is that the line stays flat — not
that any individual answer is the best obtainable.

DeepSeek via DeepInfra is chosen on that basis. A frontier tier would produce somewhat better
prose in the `reason` field and somewhat better-judged suggestions, and would price a feature
that runs unattended every Sunday against a per-account cost that grows with signups. The
recommendations are a discovery surface showing twelve cards, not a correctness-critical path
— a marginally weaker suggestion costs a user one uninteresting card, where a per-user cost
that tracks a frontier tier costs the feature its existence at the point where the product
starts working.

Two things fall out of pricing the decision this way rather than ranking models:

- **The model id is configuration; the base URL is not.** A base URL is a property of the
  provider, so it lives as a constant in `llm/registry.py`. The model id is the knob that
  actually gets turned — a provider retires an id, or a cheaper one lands — so it is
  `RECOMMENDATION_MODEL`, with **no default in code**, because a default is a claim the
  client keeps making after the id stops existing.
- **Tokens are recorded at the point of spend.** `input_tokens`, `output_tokens` and `model`
  on every `user_recommendation_set` row are what make "does this still scale" answerable
  from the database rather than from an invoice, and what make a prompt change evaluable
  across `prompt_version` values instead of guessed at.

This decision is about the *axis* the provider is chosen on. It does not pin a provider
forever: swapping one OpenAI-compatible endpoint for another that scales the same way is a
registry constant and a setting, which is precisely why the client was ported
provider-neutral.

## 3. Resolution is local-only, so ADR-0002 holds without exception

A model answers with a title and a release year. Turning that into a `catalog.show` surrogate
id is **resolution**, and it runs entirely against Postgres: fold-exact on `catalog.show.name`
within ±1 year, then fold-exact on `catalog.show_aka.title` within ±1 year, then drop and log
the raw title. The trigram indexes it reads (`ix_show_name_folded_trgm`,
`ix_show_aka_title_folded_trgm`) already exist, and the fold is `tvbf.sql_fold`, evaluated in
Postgres — there is exactly one fold and it is not reproducible in Python.

**ADR-0002 is what makes this possible rather than what constrains it.** Because the catalog is
already mirrored, the question "does this show exist" is answerable in SQL, so there is no
temptation to reach for `/search/tv` on a resolution miss and therefore no cache-miss shape to
argue about. Resolution stays behind the same guarantee every other read enjoys: no upstream
call in the request path, no timeout policy in a handler, no user-facing request contending
with the ingest jobs for the same rate limiter.

This ADR does **not** widen ADR-0002 into a ban on upstream calls from background jobs — the
migration's own `tmdb/enrichment.py` calls `/search/tv` from a batch pass, correctly. The
reason resolution does not do the same is narrower and specific to what it is resolving: a
model-authored title is not a record we hold that needs an id attached, it is a claim that may
describe nothing at all. Searching upstream for it converts a suggestion we cannot place into
a suggestion placed against whatever TMDB's relevance ranking returned, which is a worse
answer than none.

That is why a resolution failure is treated as **an outcome rather than a defect**. An
unresolved title is either a hallucination or a genuine catalog gap; both belong in the logs,
and requesting 25 recommendations to display 12 is the headroom that lets them be dropped
rather than papered over.

### No fuzzy or trigram fallback

This is the **most important negative decision in the project**, and it follows from the same
observation. A similarity threshold solves a *human search box* problem — typos — which does
not exist here: a model naming a television series either knows the show or invented one. So a
threshold buys almost no true resolutions while converting every hallucination into a
confident wrong result, and introduces a magic number nobody can calibrate against a
population of hallucinations we cannot enumerate.

### Ambiguity resolves by popularity, and this deliberately diverges from NEU-1043

When two rows fold-equal at the same year — reboots, remakes — the higher `popularity` wins,
rather than the pair resolving to nothing. NEU-1043's `tmdb_id` enrichment does the opposite,
and the divergence is the point: **the costs are not comparable, so the rules should not be
either.** There, a wrong pick silently attached a user's real watch history to the wrong show
and nothing downstream would have caught it. Here, it shows a less-likely card in a grid of
twelve, on a surface regenerated whenever the user's taste payload changes.

### There is no response cache, of any kind

The generated set is not a cache and must not acquire one. `app.user_recommendation_set` plus
`app.user_recommendation` hold **the precomputed result**: the weekly pass writes them, the
read endpoint selects the newest `succeeded` set for the user, and that is the whole path. No
TTL, no invalidation, no warm-up.

This is what makes ADR-0002 hold here without the usual tension. There is no cache miss to
handle, because there is no fetch a miss could fall back to — a user with no set sees no
section, following the same rule as every other empty surface. Introducing a cache would add
a second copy of a thing that is already precomputed and already versioned by
`generated_at`, and would give the read path a state where it is tempted to generate.

The regeneration gate is a hash, not a timer, for the same family of reasons: a payload whose
bytes are identical produces an identical answer, so an unchanged user keeps their set
indefinitely. That is deliberate product behaviour — recommendations that churn week to week
for a user who changed nothing read as randomness — and it is also why **no maximum staleness
exists**: the model recommends from its own world knowledge rather than from our catalog, so
catalog growth is not a reason to re-ask. A timestamp watermark cannot substitute for the
hash, because unwatching, un-adding and deleting a rating all remove the rows any watermark
would read.

## Consequences

- The recommendations feature has no read-time dependency on anything outside Postgres, so it
  inherits the browse path's failure characteristics rather than the provider's.
- The provider is a swappable component behind `llm/`, and the switching cost is a constant
  and a setting.
- Recommendation quality is not tunable by us in the usual sense. The levers are the taste
  payload, the prompt, and `prompt_version`; there is no ranker to adjust.
- A prompt change re-runs every user exactly once, because `prompt_version` and the model id
  are in the hash input. That is the only mechanism by which a change gets evaluated against
  real accounts.

## What this ADR does not do

**Nothing described here is built yet.** This ADR is written at the start of the project
rather than after it, which is the opposite of the house habit — ADR-0012 could point at
`orphan_retire.py` as the pass that made its rule true. It is written first because all three
decisions are load-bearing on the *first* milestone's code: the client exists to call a model
because of §1, it is budgeted and configured the way it is because of §2, and the storage
shape follows from §3.

So the table, column and module names above (`app.user_recommendation_set`,
`app.user_recommendation`, `compiled_payload`, `prompt_version`, `matched_via`) name the
project spec's design, not rows in the database. The milestones that make them real are
storage and resolution, then the weekly pass, then the surface. If one of those tickets finds
a reason to shape it differently, the *decisions* here are what it has to argue with — the
names are not.
