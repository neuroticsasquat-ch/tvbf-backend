# NEU-1178 — Dismiss a recommendation, and never recommend it again

**Ticket:** [NEU-1178](https://linear.app/neuroticsasquatch/issue/NEU-1178/backend-dismiss-a-recommendation-and-add-it-to-the-do-not-recommend)
**Parent story:** [NEU-1177](https://linear.app/neuroticsasquatch/issue/NEU-1177/remove-a-show-from-my-recommendations-and-never-recommend-it-again)
**Repo:** `tvbf-backend`
**Project:** TVBF: Maintenance
**Project spec:** `docs/specs/tvbf-personalized-recommendations-project-spec.md` (umbrella) §8, §9, §13
**Contract doc:** `tvbf-backend/docs/specs/NEU-1112-recommendations-page-contract.md` (cited by `tvbf-frontend`)
**Blocked by:** NEU-1175 (shipped — `recommendations/exclusion.py` and the read-time anti-join)
**Blocks:** NEU-1179 (the frontend control)
**Base branch:** `main`
**Status:** approved for implementation

This spec lives in the umbrella `docs/` because it is not itself cited across
repos. The cross-repo half of the change is an edit to the NEU-1112 contract
doc, which already lives in-repo for that reason (CLAUDE.md's
cross-repo-citation rule).

---

## 1. What to build

A user can remove one show from their recommendations in one action. The card
leaves the grid immediately and the next stored suggestion takes its place, and
no future weekly pass may name that show again.

The whole design is one constraint: **a dismissal must not be read as an opinion
about the show.** It reaches the never-recommend set without passing through
`taste_for_user` and it never lands in `not_liked`. `not_liked` is a taste
signal the model *generalises from*; "don't show me this card again" is a
statement about one row. Dismiss three prestige dramas you have already seen
elsewhere and a `not_liked` implementation teaches the model to stop
recommending prestige drama — a lesson the user never gave.

This reverses project spec §13, which listed dismissal as out of scope. The
reason §13 gave — *"feeds back into the prompt as negative signal"* — is exactly
what this design refuses, so the entry is superseded and its reasoning is
honoured rather than overridden. **No ADR** (D9): ADR-0013 §1's "no feedback
loop" is a claim about the *generation* mechanism, and nothing here touches it.

## 2. The table

`app.user_recommendation_dismissal`, in `app/models.py` plus one Alembic
migration on `a7c31d5e8f04`.

| Column | Type | Notes |
| -- | -- | -- |
| `user_id` | `uuid` | FK `app.user.id` `ON DELETE CASCADE`, NOT NULL |
| `show_id` | `integer` | FK `catalog.show.id` `ON DELETE CASCADE`, NOT NULL |
| `created_at` | `timestamptz` | `server_default=now()`, NOT NULL |

* **Composite primary key `(user_id, show_id)`** (D4a), matching
  `user_show_watch` — the sibling whose access pattern this one copies exactly.
  A dismissal is a pure association: the pair *is* the fact, and there is
  nothing a surrogate id could tell you about it. `user_recommendation` carries
  a UUID because a recommendation is a row with its own identity (rank, reason,
  `recovered_from`); this is not that. The PK also gives
  `ON CONFLICT DO NOTHING` its index target for free.
* **No standalone index on `show_id`** (D4b), matching `user_show_watch`. No
  query filters on `show_id` alone; the only consumer would be Postgres's
  FK-cascade check when a `catalog.show` row is deleted, and shows are
  *tombstoned*, not deleted (ADR-0005) — NEU-1146 deleted two in the entire
  migration.
* **Every constraint is named explicitly** — the PK, both FKs. Stricter than
  `user_show_watch`, which names only its cross-schema FK. The test suite builds
  these tables with `create_all` and prod builds them with Alembic, and the two
  only agree because names are stated rather than defaulted.
* `created_at` is carried so a future settings surface can list a user's
  dismissals (NEU-1177). Nothing reads it in this ticket.

**Note for `scripts/refresh_db.sh`:** it snapshots inbound FKs on the restored
schemas generically, so no edit is needed — but a `catalog` refresh must bring
`fk_urd_show` back, and that mechanism has silently dropped constraints before.

## 3. The endpoint

```
POST /me/recommendations/{show_id}/dismiss   →  204
```

`get_current_user` + `require_csrf`, matching every other mutating `/me` route.
No response body; the client refetches the list.

**`POST …/dismiss` rather than `DELETE /me/recommendations/{show_id}`** (D1).
A `DELETE` on that path reads as "remove this member from the collection this
route serves", and that is three-quarters wrong: the request *creates* a row,
the show need not be in the collection at all (AC 7), and the effect outlives
the set the collection is. It would also frame the inverse as a `POST` on the
same path — a shape we are deliberately not building.

* **Idempotent** — `ON CONFLICT DO NOTHING`, `204` either way, one row.
* **`404` for a show id no `catalog.show` row has.** No filtering on `adult` or
  `deleted_upstream_at`: a tombstoned show can be resurrected and an `adult` one
  is filtered at read time regardless, so any existing row is dismissible. Same
  as `my_shows_service.add`, which this path copies.
* **A show the user was never recommended is not an error** (AC 7). The
  never-recommend list is about future passes as much as the current set, so
  dismissing a show found by search is coherent. The endpoint does **not**
  require the show to be in the current set, and does not look at the set at all.
* Two queries plus the commit: the existence check and the insert.

### 3.1 Where the write lives (D3)

| Piece | File |
| -- | -- |
| Route | `routers/me.py`, in the Recommendations section beside the `GET` |
| Service | `app/services/recommendation_service.py` — `dismiss()` |
| Write | `app/repos/recommendation_dismissal_repo.py` (new) — `add()` |

The service is `my_shows_service.add` minus the activity emit:
`show_repo.get_by_id` → `raise NotFound()` → repo `add` → `db.commit()`, with the
router mapping `NotFound` to `404 {"detail": "not_found"}`.

**Its own repo file, not `recommendation_repo.py`.** CLAUDE.md states the repo
convention as one file per table and states the *reason* `recommendation_repo`
spans two: *"because 'the current set' is one definition"*. A dismissal is not
part of the current set — it is a fact about the user that outlives every set
they will ever be given — so the documented exception does not reach it, and
folding it in would make that module's opening sentence false about a third of
its contents.

**Not in `exclusion.py` either.** That module is a rule module which repos
import, and its one-way package edge holds only because it imports models and
nothing else. A write there would make it the owner of an HTTP-driven mutation,
and the next fifth-source author would put theirs there too.

The resulting asymmetry looks like a mistake and is not: the table's **write**
lives in a repo while its **read** lives in `exclusion.py` as a `union_all`
branch. That is exactly how the other four sources already work — `exclusion.py`
selects from `UserShowWatch` directly while `show_membership_repo` owns the
writes to it. The repo file having exactly one function is the honest
consequence.

## 4. The rule — `recommendations/exclusion.py`

The fifth source goes in `exclusion.py` **and nowhere else**, which is what makes
it change both ends at once. The module gains one `union_all` branch:

```python
select(UserRecommendationDismissal.show_id).where(
    UserRecommendationDismissal.user_id == user_id
),
```

`show_id` is NOT NULL, which is the property every branch must have: one NULL in
the operand makes the `NOT IN` anti-join return nothing at all, silently emptying
the recommendations page.

### 4.1 The rename (D2)

```
show_ids_with_a_record       →  show_ids_never_to_recommend
load_show_ids_with_a_record  →  load_show_ids_never_to_recommend
```

Two call sites (`recommendation_repo.list_current_recommendations`,
`payload.build_payload`) and the tests.

The old name is about to stop being true. AC 7 is explicitly "dismissing a show
the user was never recommended succeeds" — a dismissal can name a show the user
has never met, which is the opposite of having a record for it. Keeping the name
leaves the module's central function misdescribing its own contents, and the next
reader finding a dismissal branch inside `show_ids_with_a_record` has to decide
whether it is a bug.

The rejected alternative is worth recording: keeping a four-source
`show_ids_with_a_record` and composing a union beside it. That puts three
functions in a module two callers need one of, and the moment a third caller
appears it can reach for the narrow one and quietly under-suppress — the
two-expressions failure mode this module exists to close, reintroduced one level
down.

The cost is that the code moves away from project spec §8's phrase "has a record
for". That phrase is a rule about *generation*; this function is now also the
read-time suppression, so it has outgrown the sentence. The docstring keeps both
justifications: four of the five branches are §8's record sources, the fifth is a
dismissal, and the union is the never-recommend set.

## 5. Read-time suppression

Nothing changes in `recommendation_repo.list_current_recommendations` beyond the
renamed call — the anti-join already covers the new branch. The properties
NEU-1175 established all hold unchanged and are re-asserted here because they are
what AC 1 actually tests:

* The set is **never mutated or deleted**. Immutability is what makes the weekly
  swap atomic (§9) and what keeps `raw_response` usable as the record of what the
  model said.
* **`rank` is never renumbered.** Values stay non-contiguous, exactly as the
  `adult` filter already leaves them.
* The `[:DISPLAY_LIMIT]` slice off the front of the rank order is what promotes
  the next suggestion, for free.
* **Fewer than twelve is a normal answer.** No backfill from an older set.
* **The query count does not grow per row**: still three queries (the rows,
  plus `hydrate_show_refs`' pair), one when nothing survives.

## 6. Exclusion wiring — the payload

`payload.build_payload` calls the renamed loader and nothing else changes: the
dismissed ids arrive in `excluded_show_ids`, `titles_for_ids` is already asked for
the union of tier ids and the exclusion set, and `_exclude_rows(excluded -
shown_ids, titles)` puts a dismissed show in the `exclude` group **because no
tier covers it**. So the model is *told* not to name it rather than only being
filtered afterwards. The pass's query count is unchanged.

Three consequences are free and are asserted in tests rather than assumed:

* `liked_count`, `interested_count`, `interested_before_cap` and the generation
  floor are untouched — `taste_for_user` never sees a dismissal (AC 4).
* The payload's bytes change, so the hash changes and that user's next run is not
  skipped as unchanged (AC 5). That is the correct blast radius: only users who
  dismissed something regenerate.
* A dismissed show that *also* reached a tier (dismissed and in My Shows) stays
  in its tier group and out of `exclude`, since `exclude` is `excluded -
  shown_ids`. Nothing special is needed for that case.

## 7. The prompt, and `PROMPT_VERSION` 5 (D5)

Two clauses in `INSTRUCTION` become false for a dismissed show the user has
never seen, and the second makes the ban grammatically *derive* from the false
premise:

> `"exclude" is a plain list of further series they already have…`
> `Every series named anywhere in the user message is one this person already has — … — so none of them may appear in your answer.`

They are reworded to the minimum that removes the premise, leaving everything
else byte-identical — in particular leaving `"drop it without comment and give
the next best one you have not used"` untouched, since removing that mechanism is
exactly what broke `PROMPT_VERSION` 2:

```python
'"exclude" is a plain list of further series to leave out, with the '
'fields "exclude_columns" names and no viewing data — it is there only so '
"you can avoid them.\n"
...
"Every series named anywhere in the user message — in \"liked\", in "
'"not_liked", in "interested" or in "exclude" — is one this person must not '
"be recommended, so none of them may appear in your answer. "
```

**`PROMPT_VERSION` bumps to `"5"` in the same commit**, per CLAUDE.md's rule.

Why change it at all, when the operative sentence is the ban and the ban is
unchanged: this project's own record shows the model reasoning *from* these
justification clauses rather than merely obeying the imperative. Version 2's
docstring records the model noticing the user already had a show and redirecting
around it, and then — when the narration was banned — dropping the redirect
instead of the show. A model that finds something in `exclude` it is confident
the user has not seen has, on that record, a plausible route to deciding the
premise does not apply. Dismissal is permanent, so that failure is the expensive
one.

The bump's cost is near zero here: 3–5 accounts regenerate once next Sunday, and
a bump is the only way a prompt change is evaluated against real users at all.

## 8. The pass's exclusion counter (D10)

`jobs/weekly_recommendations._Attempt.excluded` inherits the same problem: it is
documented as *"Named titles that resolved onto a show the user already has
(§8)"* and `complaint` renders `"{n} of {m} titles were series the user already
has"`.

* **A dismissal counts toward `IGNORED_EXCLUSION_FRACTION` identically.** No new
  threshold and no sixth counter. A dismissed show is in `exclude` and named to
  the model exactly like the other four sources, so a model naming it is the same
  disobedience the guard was built for. The guard is deliberately blunt — >9/10
  of named titles is 23 of 25, "the instruction was ignored wholesale" — and a
  heavy dismisser cannot realistically drift into it; if one does, the retry is
  the correct response.
* **The language widens** to "series this person must not be recommended", in
  `excluded`'s docstring and in `complaint`'s string. These are a docstring and a
  log line, not the request/response contract, so no `PROMPT_VERSION`
  consequence. Leaving them is how the next reader concludes the guard covers
  only My Shows.

## 9. Scope of "never recommend it again" (D6)

**Dismissal is scoped to `GET /me/recommendations` and the weekly pass. Trending,
most anticipated, similar shows, search and browse are untouched.** Written as an
explicit negative because the story's wording invites the opposite.

Those surfaces are **catalog facts, not personal suggestions** — "what is
trending this week" is a statement about the world, and removing yourself from it
silently is a different feature with a different name. Each would need a per-user
anti-join, and `/similar` would have to give up its cacheable router-level header
for it. Most decisively: **this ticket ships no un-dismiss.** Under the wider
rule one tap permanently removes a show from browse-adjacent surfaces with no way
back, which turns a cheap one-tap grid action into something needing a confirm
dialog — the cost NEU-1179 is explicitly trying to avoid. A user must still be
able to *find* a dismissed show, precisely because they cannot un-dismiss it.

## 10. Adjacent surfaces, deliberately unchanged (D8)

* **`scripts/refresh_db.sh` anonymisation.** `user_recommendation_dismissal` is
  **not** added to the `TRUNCATE` list. That list holds `watch_archive` and the
  recommendation-set pair because `compiled_payload` is a second copy of the
  user's watch history; a dismissal is `(user_id, show_id, created_at)` and
  nothing more — the same class as `user_show_watch`, which is deliberately kept
  so a refreshed local database has data to develop against.
* **`/me/export`.** Unchanged. Its document shape is locked to `account` /
  `my_shows` / `watch_history` and already omits ratings, connections, feedback
  and recommendations; adding dismissals would make it the one non-history table
  in a document carrying none of the others. Widening the export is its own
  ticket. That the user cannot yet *see* their dismissal list is a known gap of
  NEU-1177, whose answer is a future settings surface, not a JSON download.

## 11. Acceptance criteria

1. Dismissing a show in the current set removes it from `GET /me/recommendations`
   and the next stored suggestion appears, ranks unchanged.
2. Dismissing the same show twice is `204` both times and leaves one row.
3. A dismissed show appears in the next payload's `exclude` group and in no tier
   group.
4. `liked_count` / `interested_count` / `interested_before_cap` and the
   generation floor are unchanged by a dismissal — it is not a taste signal.
5. The payload hash after a dismissal differs from before it.
6. A dismissed show is never named by a subsequent pass, and if the model names
   it anyway the §8 filter drops it and counts it into `_Attempt.excluded`.
7. Dismissing a show the user was never recommended succeeds and still excludes
   it from future passes.
8. One user's dismissals never affect another's list or payload.
9. Nothing about a dismissal reaches `app.activity_event`,
   `app.user_show_rating`, or the friend feed.
10. The endpoint requires a session and a CSRF token, and `404`s on a show id no
    `catalog.show` row has.
11. `PROMPT_VERSION` is `"5"` and the two reworded clauses read as §7 specifies.
12. Trending, most anticipated, similar-shows, search and browse are unaffected
    by a dismissal.
13. The read costs three queries and the payload's query count is unchanged.

## 12. Tests

| Where | What |
| -- | -- |
| `tests/integration/recommendations/test_exclusion.py` | The fifth branch on its own; a dismissal with no other record; both call shapes agreeing after the rename; one user's dismissal invisible to another (AC 8) |
| `tests/integration/app/repos/test_recommendation_repo.py` | A dismissed row suppressed from the current set; ranks unchanged and non-contiguous; the set's rows still present afterwards (AC 1, 9) |
| `tests/integration/routers/test_me_recommendations.py` | The endpoint: `204`; idempotent and one row (AC 2); `404` on an unknown show; no session / no CSRF (AC 10); dismissing a show never recommended (AC 7); end-to-end AC 1 — dismiss, refetch, replacement appears; no `activity_event` row written (AC 9); the query-count assertion on `test_trending.py`'s `before_cursor_execute` pattern (AC 13) |
| `tests/integration/recommendations/test_payload.py` | AC 3, AC 4, AC 5 |
| `tests/unit/recommendations/test_prompt.py` | The reworded clauses and `PROMPT_VERSION` (AC 11) |
| `tests/integration/jobs/test_weekly_recommendations_pass.py` | AC 6 — a model naming a dismissed show has it dropped and counted into `excluded` |

Two placement calls, made deliberately:

* **The endpoint's tests extend `test_me_recommendations.py`** rather than taking
  a new file. NEU-1112's doc names that file as the contract-test home, §7 below
  makes the dismiss endpoint part of that contract, and AC 1 is naturally one
  test that dismisses and then refetches.
* **No `test_recommendation_dismissal_repo.py`.** The repo is one
  `ON CONFLICT DO NOTHING` insert; its only interesting property is idempotency,
  which AC 2 exercises through the route where it matters.

## 13. Documentation

**`tvbf-backend/docs/specs/NEU-1112-recommendations-page-contract.md`** (D7) —
the frontend cites this by URL and NEU-1179 will read it.

* §4.1 is reframed from "a show the viewer already has a record for is
  suppressed" to the **never-recommend set**, with the dismissal as a fifth row
  in the sources table. The read rule's shape is otherwise unchanged: ranks stay
  non-contiguous, fewer than twelve is normal, empty is `200
  {"recommendations": []}`.
* §5's `No dismissal / "not interested". Out of scope with reasons (§13)` bullet
  is **deleted** and replaced by a section specifying
  `POST /me/recommendations/{show_id}/dismiss` — auth, CSRF, `204`, idempotence,
  `404`, the fact that the show need not be in the current set, and that there is
  **no un-dismiss**.
* A note that a client must not re-implement the suppression rule, and that a
  dismissal is one of the actions after which the grid is stale.

Everything lands in this one doc rather than a second in-repo contract: §4.1 and
§5 both have to change regardless, and splitting the endpoint out would leave the
frontend reading "dismissal does not exist" in one file and its spec in another.

**`tvbf-backend/CONTEXT.md`** — two new entries in the Recommendations section
and one correction:

* **Never-recommend set** — the five sources, defined once in
  `recommendations/exclusion.py`, enforced at both ends (the weekly payload and
  the read path). _Avoid_: blocklist, exclusion list.
* **Dismissal** — a user removing one show from their recommendations. An
  *exclusion*, deliberately not a taste signal: it never reaches
  `taste_for_user`, never lands in `not_liked`, and the model is never told the
  user disliked anything. Permanent and, today, not reversible.
  _Avoid_: not interested, negative rating, thumbs down.
* **Taste payload** — the clause "every row in it names a show the user already
  has a record for" is corrected; `exclude` can now carry a show the user has
  never seen.

**`tvbf-backend/.claude/CLAUDE.md`** —

* `recommendations/exclusion.py`'s module-map line and the "enforced twice, from
  one definition" non-obvious-pattern entry both name the renamed functions and
  the fifth source.
* The `app` schema's table list gains `user_recommendation_dismissal`.
* The `/me` endpoint list gains `POST /me/recommendations/{show_id}/dismiss`.
* `app/repos/` gains `recommendation_dismissal_repo.py` in the module map.
* The frontend-conventions bullet on invalidating `["me-recommendations"]`
  already anticipates this ticket by name; it needs the dismissal folded in as a
  real fifth source rather than a forward reference.

**No ADR** (D9) — see §1.

## 14. Out of scope

* **Un-dismissing.** "Never again" is the feature as asked for. The row carries
  `created_at` so a future settings surface can list them; a hidden permanent
  list with no way back is the thing to watch, and it is NEU-1177's to follow up.
* **A surface listing a user's dismissals.** Same follow-up.
* **The frontend control** — NEU-1179.
* **Dismissal as negative taste signal** — refused on purpose (§1), not deferred.
* **Widening `/me/export`** and **anonymising the new table** (§10).
* **Suppressing dismissals on any other surface** (§9).
