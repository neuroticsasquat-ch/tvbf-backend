# NEU-1145 — Airdates one day early since the TMDB cutover

**Ticket:** [NEU-1145](https://linear.app/neuroticsasquatch/issue/NEU-1145/backend-apple-tv-episodes-show-an-airdate-one-day-early-since-the-tmdb)
**Repo:** `tvbf-backend` (with a `tvbf-frontend` half — see §10)
**Project:** TVBF: TMDB Migration · Milestone 5, Migration & cutover
**Related:** [NEU-1031](NEU-1031-tmdb-coverage-audit.md) §6, [NEU-1146](NEU-1146-retire-the-tv-maze-orphan-rows.md), NEU-1047, NEU-1147
**Status:** approved for implementation

This spec lives in-repo rather than in the umbrella `docs/` because its frontend
half is a separate ticket in a different repo that has to cite it by URL: it
restores a TVmaze attribution that NEU-1147 removed two days earlier, and the
licence reasoning that justifies the reversal is §6 of this document.

---

## 1. What this is for

Since the TMDB cutover, Apple TV+ and Prime Video episodes display an airdate one
day earlier than the date the episode actually becomes available to a US viewer.
Reported by the operator 2026-08-13 against Silo, Lucky and Ted Lasso — all
currently airing, all in My Shows, all wrong every week.

This ticket makes those dates correct, automatically, with no recurring human
step.

---

## 2. What was measured

Every number below was measured against production and the live TMDB API on
2026-08-14. They are recorded here because three of them overturn the hypotheses
the ticket was written around, and a later reader will otherwise re-derive them.

### 2.1 Our mirroring is faithful — ruled out, not assumed

`catalog.episode.air_date` in production matches the TMDB API exactly on every
spot-check: Severance S1E1 `2022-02-17`, S2E1 `2025-01-16`, Silo S1E1
`2023-05-04`, The Boys S1E1 `2019-07-25`. Nothing in our ingest, storage or
serialisation alters the value.

### 2.2 TMDB models no regional airdate — the ticket's hypothesis 2 is dead

A TMDB episode object carries exactly one date key. The full key set is
`air_date`, `episode_number`, `season_number`, `episode_type`, `runtime`,
`production_code`, `name`, `overview`, `still_path`, `id`, `crew`,
`guest_stars`, `vote_average`, `vote_count`. There is no time, no timezone, no
per-region variant — TMDB's release-date-by-region model is movies-only.

The value is also **global**: re-fetching the same episodes under
`language=en-US`, `en-GB`, `de-DE`, `ja-JP` and `region=US`, `region=NZ` returns
an identical `air_date` every time.

So there is no richer upstream field to consume. This is the finding NEU-1031 §6
has to be reconciled against, and it holds: TMDB carries no airtime, and it
carries no regional date either.

### 2.3 It is not a timezone artifact in our stack, and not one in TMDB's either

`catalog.episode.air_date`, `catalog.season.air_date` and
`catalog.show.first_air_date` / `last_air_date` are all bare `Date` columns. There
is no instant to convert.

Nor can it be a rendering artifact upstream. Apple TV+ originals all drop at the
same moment relative to US Eastern — 9pm PT / midnight ET / 05:00 UTC — so a
clock offset applied by TMDB would shift *every* Apple show equally. It doesn't:
Silo is shifted in all three seasons and Slow Horses in none of five. A clock
cannot be selective per show. Combined with §2.2's locale-invariance, the
conclusion is that TMDB stores a date a contributor typed.

### 2.4 It is not Apple-specific, and it is not systematic

Comparing TV Maze's snapshotted airdates in `app.watch_archive` (the NEU-1029
backstop, captured pre-cutover) against current TMDB values, across all archived
episode watches:

| network | agrees | one day early | other |
|---|---|---|---|
| Apple TV | 104 | **198** | 10 |
| Prime Video | 35 | **93** | 0 |
| Netflix | 1,056 | 13 | 0 |
| ABC, CBS, FOX, HBO, HBO Max, Max, Hulu, NBC, FX, AMC, Peacock, Disney+, Paramount+, Showtime, STARZ, Syfy, TBS, The CW, USA, and every other network in the sample | all | **0** | — |

And the shift splits *within* a single show, uniformly per season:

| show | seasons agreeing | seasons one day early |
|---|---|---|
| Ted Lasso | 1, 2 | 3, 4 |
| Servant | 1, 2, 3 | 4 |
| The Afterparty | 1 | 2 |
| The Boys | 4 | 1, 2, 3 |
| **Slow Horses** | **1, 2, 3, 4, 5** | — |
| Silo | — | 1, 2, 3 |
| Severance | — | 1, 2 |

Silo and Slow Horses are indistinguishable on every attribute we mirror — both
Apple TV, US, `en`, Returning Series, Scripted, in production — and overlap in
time. Nothing on the show row separates a shifted show from an unshifted one.

### 2.5 The mechanism

Both values are the local date *somewhere in the US* at the moment of release:
9pm PT Thursday is midnight ET Friday. The shifted entries record the **Pacific**
date; the unshifted ones record the **Eastern** date. TMDB has no convention
here, so which one a season carries depends on who entered it.

The industry convention that creates the ambiguity is visible elsewhere: the
iTunes Search API stamps its TV release dates at `T08:00:00Z`, which is midnight
Pacific. (It was evaluated as a possible oracle and rejected — it carries no
Apple TV+ originals at all; "Ted Lasso" returns zero results.)

### 2.6 Why no rule can derive the correction

The episode weekday is the only signal in the data, and it discriminates for one
network only:

| network | TMDB weekday | agrees | one day early |
|---|---|---|---|
| Apple TV | Tue | 0 | 114 |
| Apple TV | Thu | 3 | 83 |
| Apple TV | Wed | 18 | 0 |
| Apple TV | Fri | 83 | 0 |
| Prime Video | Tue | **9** | 17 |
| Prime Video | Thu | **8** | 76 |
| Prime Video | Wed | 15 | 0 |
| Prime Video | Fri | 2 | 0 |

For Apple TV+ a "Tue/Thu ⇒ Pacific" rule would be right on 197 of 200 (the three
exceptions being Servant S1's genuine Thanksgiving-Thursday premiere). For Prime
Video the same rule corrupts 17 currently-correct rows to fix 93, because Prime
really does release on Tuesdays and Thursdays.

**Any per-network, per-era or weekday-derived offset is therefore rejected.** It
would redistribute errors rather than remove them.

---

## 3. Approaches considered and rejected

| approach | why rejected |
|---|---|
| **Fix the frontend date parsing** | The SPA already parses into a local date correctly (`const [y,m,d] = iso.split("-").map(Number)`). Re-verified. Not the cause. |
| **Accept and document the limitation** | The ticket's own fallback branch, and explicitly rejected by the operator: three tracked, currently-airing shows are wrong every week. |
| **Per-network or per-era offset rule** | §2.6. Redistributes errors. |
| **Re-source airdates from TV Maze wholesale** | Contradicts ADR-0012, and the `tvmaze` schema was dropped by NEU-1051. |
| **Hand-authored offsets, with or without a proposer queue** | Rejected by the operator: any recurring manual step will rot, and new seasons land weekly. |
| **LLM confirmation at ingest** | An exact calendar date is the worst possible thing to ask a model for. Failures are silent and unfalsifiable across 6.5M rows. Not used, not even as a fallback. |
| **iTunes Search API as oracle** | Carries no Apple TV+ originals (§2.5). |
| **Trakt as oracle** | Viable — `first_aired` is a true UTC instant, and it is not copyleft. Rejected for now because it needs a new credential and its accuracy is unverified by us, whereas every accuracy claim in this document is TVmaze-backed. Reconsider if TVmaze becomes unavailable; a spike would need to reproduce §2.4's 440-row comparison first. |

---

## 4. Design

### 4.1 The offset table

```
catalog.air_date_offset(
    show_id       bigint  not null references catalog.show(id) on delete cascade,
    season_number integer null,
    offset_days   integer not null,
    ...
)
```

Keyed on `(show_id, season_number)` — the pair `catalog/seasons.py` already
treats as a season's read-path identity. **Not** a column on `catalog.season` and
**not** keyed on `season.id`, for two measured reasons: `catalog.episode.season_id`
is nullable (`ON DELETE SET NULL`, so a pruned season leaves parentless episodes),
and `catalog.season` deliberately carries no `UNIQUE (show_id, season_number)`, so
a doubled season would make the offset ambiguous.

A separate table also makes "the ingest never writes offsets here" structural
rather than a rule someone has to remember — the same reasoning that keeps
`rate_budget` and `ingest_run` in tables of their own.

`season_number IS NULL` denotes a show-wide default that a numbered row overrides.
**The job never writes it**; it exists solely as an operator escape hatch, which
keeps the table's two possible writers distinguishable.

### 4.2 The raw upstream value

New column `catalog.episode.tmdb_air_date`, **populated only when it differs from
`air_date`**. NULL means "no correction applied — `air_date` is untouched TMDB",
which is true for essentially all of the 6,026,676 dated episode rows and keeps
NEU-1031's claim that `air_date` = TMDB's `air_date` literally true for them.

It buys three things: "every row we altered" becomes an indexed predicate for
verification; changing an offset becomes a local SQL update instead of a TMDB
re-fetch; and un-doing the whole feature is
`air_date = tmdb_air_date WHERE tmdb_air_date IS NOT NULL`.

### 4.3 The write path

**`upsert_series_payload` applies the offset as it writes**, setting `air_date`
and `tmdb_air_date` together from the payload value. One offset lookup per show
is added to the ingest path.

This is load-bearing for idempotency and must not be moved to read time or to a
post-hoc re-apply step. Because both columns are only ever written as a pair from
a value in hand, a re-run cannot double-apply a shift, and a legitimate upstream
change of exactly ±1 day cannot be mistaken for a correction already applied — the
failure a "re-apply after the delta" design has no way to distinguish.

Correcting on the way in is also what keeps every reader right for free. A
read-time correction would have to be threaded through browse, `/me/upcoming`,
Watch Next, the season and episode pages, and `episode_repo`'s
`air_date <= today` aired filter — the many-call-sites asymmetry the specials
ledger exists to police, where the site that forgets has no test.

### 4.4 The reconciliation job

`python -m tvbf.jobs.airdate_reconcile`, a second Coolify scheduled task through
`jobs/scheduled.py:run_scheduled_delta`:

- `kind='airdate_reconcile'`, added to `ck_ingest_run_kind` NOT VALID.
- Its own `HEALTHCHECK_AIRDATE_URL` deadman. **Not** folded into
  `catalog_update`: CLAUDE.md's rule stands — one check fed by two tasks lets
  either keep it alive while the other quietly stops. Folding in would buy
  guaranteed ordering after the delta, which §4.3 makes unnecessary, since a
  delta can never clobber a correction.
- A `catalog.ingest_run` row, so it is pollable via the unfiltered
  `GET /admin/ingest/{run_id}`.
- Exit code is the result: 0 means the pass ran and succeeded.

**Oracle:** TVmaze. Keyless and free. Show lookup is
`/lookup/shows?imdb=<imdb_id>` falling back to `?thetvdb=<tvdb_id>` — we already
store both, and 557 of 560 tracked shows carry at least one. Episodes come from
`/shows/{id}/episodes` in a single request, so a show costs ~2 requests.

The job is **network-agnostic**. Choosing an oracle removed the need for the
weekday heuristic entirely, so there is no network allowlist and no Apple/Prime
asymmetry in the code.

**Work list:** shows tracked by any user **or** holding a future-dated episode.
~3,500 requests, ~30 minutes per run at TVmaze's published ~20 calls/10s.

> **Corrected by [NEU-1149](NEU-1149-scope-the-airdate-work-list-to-change.md),
> twice.** This paragraph originally read that the two halves "nearly coincide"
> — 1,762 in scope against 1,767 with a future episode and 560 tracked. Measured
> against production on 2026-08-14 they are nearly **disjoint**: 560 tracked,
> 1,787 with a future episode, **27 in both**, union 2,320, of which 1,772 carry
> an external id and are checkable. The number in scope was right; the overlap
> was not, and what it hid is that the run is two populations doing two
> different kinds of waste (NEU-1149 §1.1).
>
> It also read "**No watermark** — the full list runs every night, ... and
> re-checking everything is what makes the job self-healing when a season's
> dates change upstream." That was true when written and NEU-1149 makes it
> false. There is now a per-show `last_reconciled_at`, and a show in scope is
> reconciled only when something could have changed for it — it has never been
> done, TMDB touched it since, it is still airing — or its sweep turn has come.
> **The sweep is what buys the self-healing property back**, on a documented
> weekly cadence, amortised by `show_id` bucket so no night spikes. See NEU-1149
> §5.

The scope predicate lives in one place. Widening it to the full 115,731 shows
carrying an external id is a one-line change plus a resumable watermark, and was
deliberately **not** done: ~32 hours of sustained traffic against a free, keyless,
unfunded API for a five-user app is poor etiquette, and being blocked would take
the fix down with it.

**Rate limiting:** through `get_rate_limiter("tvmaze", Budget(...))` on its own
`catalog.rate_budget` row — the keyed-bucket pattern that makes a new upstream a
row rather than a rewrite. (NEU-1050 retired this exact bucket; it returns.) The
three standing rules apply unchanged: never sleep holding the row lock, use
`clock_timestamp()`, and fail closed when the bucket is unreachable. `Bucket.table`
and `Bucket.key_column` may only come from the module-level registry.

**Out-of-scope shows are logged**, per the no-silent-caps rule: shows with no
external id, no TVmaze counterpart, and seasons refused by §4.5.

### 4.5 The trust rule

An offset is written for a `(show_id, season_number)` only when **all** of:

1. Every episode carrying a date on both sides, whose `(season_number,
   episode_number)` pairing is unambiguous on both sides, differs by the **same**
   amount;
2. that amount is exactly **±1**;
3. at least **two** such episodes exist.

Anything else writes nothing and is logged.

The **clamp is principled, not defensive**: the bug we are authorised to correct
is timezone-shaped, and a timezone artifact can only ever produce ±1. Any other
delta is a different disagreement, and correcting it would make us mirror
TVmaze's schedule instead of fixing TMDB's coast — scope creep with no bound.

**Shrinking S3 is the worked example.** TMDB: E1 `2026-01-27`, E2 `2026-02-03`,
E3 `2026-02-10`, weekly. TVmaze: E1 `2026-01-28`, **E2 `2026-01-28`**, E3
`2026-02-04` — a two-episode premiere TMDB spreads across two weeks. The
per-episode deltas are `+1, +6, +6, …`. Both the clamp and the unanimity rule
reject it independently, where a job trusting the oracle's delta would have moved
nine episodes by six days.

The cost is real and accepted: Shrinking S3's E1 stays one day early
indefinitely. A per-episode correction would fix it, and is rejected — it would
leave E1 on Wednesday while E2–E10 stay Tuesday, and a season that contradicts
itself in a list is a worse artifact than one that is uniformly one day early.
A consistently-wrong season also surfaces in the refusal log, where a human can
find it; a hybrid one looks deliberate.

This is the same asymmetry `enrichment.py` draws (every ambiguity resolves to
unmatched) and `orphan_retire` repeats (1:1 uniqueness required on both sides).

### 4.6 The four grains

The offset applies at every grain that surfaces a date, or the correction
manufactures a contradiction on a single page — Silo's detail page would read
"Premiered May 4, 2023" directly above a season 1 whose episode 1 says May 5.

| column | API field | takes the offset of |
|---|---|---|
| `catalog.episode.air_date` | `EpisodeOut.airdate` | its own season |
| `catalog.season.air_date` | `SeasonOut.premiere_date` | its own season |
| `catalog.show.first_air_date` | `ShowDetail.premiered` | **season 1** |
| `catalog.show.last_air_date` | `ShowDetail.ended` | the season holding the last dated episode |

Keying the show-grain dates to the season they derive from is what makes them
correct per show, and **Ted Lasso is the proof**: S1–S2 were Eastern-entered and
S3–S4 Pacific-entered, so its true premiere `2020-08-14` must not move while its
last-aired date must. A blanket per-show offset breaks the premiere; a unanimity
rule declines to fix the last-aired date.

Note for the record: a ±1 shift on `first_air_date` can cross a year boundary for
a Dec 31 / Jan 1 premiere, moving the show in the live `?sort=premiered` key and
changing its displayed year. That is correct behaviour, not a regression. The
migration's tier-3 `first_air_date` year check would have been reading a
corrected value had it run after this — irrelevant, since enrichment is spent,
but recorded so nobody re-derives it.

---

## 5. What `air_date` now means

`catalog/models.py` must say so at the column, per the ticket's fourth acceptance
criterion:

> `air_date` is the date the episode became available **to a US Eastern viewer**,
> which is TMDB's `air_date` corrected by `catalog.air_date_offset` where one
> applies. `tmdb_air_date` holds the uncorrected upstream value, and is NULL when
> no correction was applied — which is the overwhelming majority of rows.

The same note belongs on `season.air_date` and `show.first_air_date` /
`last_air_date`, and in CLAUDE.md's non-obvious-patterns section.

---

## 6. Licence and attribution

TVmaze data is CC BY-SA 4.0. NEU-1146 is deleting 782k rows specifically to shed
that obligation, and NEU-1147 removed the TVmaze credit from the footer on
2026-08-12. This ticket re-admits a TVmaze-derived value, so the credit returns.

The extraction is deliberately minimised to match: we store **one integer per
`(show, season)`** — plus, since NEU-1148, **one show id per show** — and never
copy TVmaze's dates, titles, numbering or any other field. The catalog itself
remains free of TV Maze rows, which is what NEU-1146 is actually for — the
operator's stated objection was 800k phantom rows, not attribution.

The frontend half restores a trimmed TVmaze credit alongside the TMDB one. It is
a separate ticket in a separate repo (§10).

### 6.1 Amended by NEU-1148 — the cached show id

[NEU-1148](NEU-1148-cache-the-tv-maze-show-id.md) caches the oracle's id for a
show in `catalog.airdate_show_state`, so the nightly pass stops re-deriving it
with a `/lookup/shows` request every night. That is a **second integer, per
show**, taken from TVmaze, and the paragraph above is amended to say so rather
than left contradicting the code. The amended position, in order of what it
rests on:

1. **The attribution condition is already satisfied.** The credit is in the SPA
   footer. Even on the most conservative reading — that a share-alike obligation
   attaches to an identifier — we are compliant, and nothing turns on the next
   point.
2. **A show id is an identifier, not creative expression.** It is a bare fact
   about which record corresponds to which series, carrying none of the authored
   content the licence exists to govern.
3. **The extraction is still minimised, and the principle still binds.** One
   offset per `(show, season)`, one id per show, and nothing else — no dates, no
   titles, no numbering, no synopses, no images. The reason to keep minimising is
   not licence compliance alone: NEU-1146 deleted 782k rows because the operator
   objected to phantom rows in the catalog, and that objection is independent of
   any licence.

**The spine/sidecar distinction, made explicit.** "The catalog itself remains
free of TV Maze rows" above now has two tables sitting awkwardly beside it. The
line actually drawn — by ADR-0012, by NEU-1148 §2, and by `air_date_offset`
before either — is:

> `catalog.show`, `catalog.season` and `catalog.episode` hold no TV
> Maze-derived value at any grain. Sidecar tables in the `catalog` schema
> (`air_date_offset`, `airdate_show_state`) hold derived integers, and that is a
> different thing from the catalog holding TV Maze rows.

Making it explicit is what stops a later reader taking this section as "no TV
Maze value may exist inside schema `catalog`" and either breaking the rule
silently or being blocked by it wrongly.

---

## 7. Acceptance criteria

1. **The cause is stated.** It is TMDB's own data: a contributor-entered calendar
   date that records the Pacific rather than the Eastern day for some seasons.
   Not TMDB's regional model (there is none, §2.2), not our mirroring (§2.1).
2. **Silo, Lucky and Ted Lasso display the correct airdate** for a US viewer, and
   are hand-verified against Apple's published schedule before the ticket closes.
3. **The 440-row regression check passes.** Re-run §2.4's `app.watch_archive`
   comparison after the job's first pass: the 198 Apple and 93 Prime shifted rows
   now agree, and the 104 + 35 already-correct rows are untouched. This is a free
   labelled test set and it is the ticket's real proof.
4. **No manual step recurs.** A newly tracked show, or a new season of a tracked
   show, is corrected by the next scheduled run with no human involvement.
5. **The job refuses rather than guesses.** Shrinking S3 gets no offset, and
   appears in the refusal log with its per-episode deltas.
6. **`catalog/models.py` says what `air_date` means** (§5), and CLAUDE.md records
   the pattern.
7. **The ingest is idempotent under the correction.** Re-running the full pass or
   a delta over a show with an offset leaves `air_date` and `tmdb_air_date`
   unchanged, and a legitimate upstream ±1 day change is picked up rather than
   swallowed. Test this explicitly — it is the one failure mode §4.3 exists to
   prevent.

---

## 8. Testing notes

- Route tests use `AsyncClient(ASGITransport(app=app))`, never `TestClient`.
- Fixtures seeding `catalog` need `await session.flush()` between parent and
  child inserts — there are no `relationship()` declarations.
- A test that mutates a seeded `Show` and then reads a path needs
  `await session.refresh(show)`, because `is_ended` is a generated column.
- Tests asserting a row after an upsert need
  `execution_options={"populate_existing": True}`.
- Autouse fixtures must not request `monkeypatch`.
- TVmaze must be stubbed in the suite; no test may make a live call.
- New `result.rowcount` sites need `# type: ignore[attr-defined]`, and the
  CLAUDE.md list of such files needs updating (`grep -rln 'rowcount.*type: ignore' src/`
  is authoritative).
- Run `task format` before committing — pre-commit checks formatting but does not
  fix it.

---

## 9. Out of scope

- **Widening the work list to the full catalog** (§4.4). One-line change plus a
  watermark when it is wanted.
- **TMDB's two-episode-premiere error on Shrinking S3.** A real upstream defect,
  a different bug, deliberately not fixed here. The refusal log is where such
  seasons surface.
- **Contributing corrections upstream to TMDB.** Would fix it for every consumer
  including the third-party app in the ticket, but TMDB exposes no metadata-write
  API, so it is unbounded manual work. Available to the operator; not a
  deliverable.
- **Trakt as an alternative oracle** (§3).
- **Any airtime.** TMDB carries none and TVmaze returns an empty `airtime` with a
  synthetic noon-UTC `airstamp` for streaming shows, so there is no time-of-day
  to recover. NEU-1031 §6's loss stands.

---

## 10. The frontend half

A separate `tvbf-frontend` ticket restores a trimmed TVmaze CC BY-SA credit to
the footer in `AppShell.tsx`, alongside the TMDB attribution NEU-1049 requires
verbatim. It reverses the removal NEU-1147 made, and must cite §6 of this
document for the reason.

> **Shipped 2026-08-14** in `tvbf-frontend`
> [#181](https://github.com/neuroticsasquat-ch/tvbf-frontend/pull/181), without a
> ticket of its own. Two things about it differ from what this section
> anticipated, both downstream of §6.
>
> **It is not a reversal of NEU-1147.** That credit read *"Some legacy title
> data is © TVmaze"*, which is now false — §6 is explicit that no TVmaze date,
> title or numbering is stored, and that the extraction was minimised to a
> derived integer precisely so the obligation would stay small. Restoring the
> string verbatim would assert the thing §6 promises we do not do. The shipped
> wording is *"Airdate corrections are derived from data provided by TVmaze,
> licensed under CC BY-SA 4.0."* — which also supplies the
> indication-of-modification CC BY-SA asks for alongside attribution and the
> licence link.
>
> **It sits on its own line** rather than trailing the TMDB sentence, so neither
> attribution reads as a qualifier on the other; TMDB's required notice is
> untouched and still asserted verbatim by its own test. The footer assertion
> has now been inverted twice — presence, absence, presence — which is the
> argument for NEU-1147 having replaced it with its inverse rather than
> deleting it.

No other frontend change is needed: the SPA's date parsing is already correct
(§3), and the API contract is unchanged — the same four fields carry the same
types, with corrected values.
