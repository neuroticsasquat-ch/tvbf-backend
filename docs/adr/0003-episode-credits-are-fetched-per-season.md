# Episode credits are fetched per season, by the show axis

**Status:** accepted (2026-08-04)
**Amends:** [ADR-0001](./0001-tvmaze-second-ingest-axis.md)

Every episode-level credit — guest cast and episode crew alike — is fetched from `/seasons/{id}/episodes?embed[]=guestcast&embed[]=guestcrew`, one request per season, as part of the show axis. The person axis no longer writes credits of any kind.

ADR-0001 made `person` a second ingest axis on the strength of one claim: that episode guest cast was unreachable from the show side at acceptable cost, because `/shows/{id}/episodes?embed[]=guestcast` returns nothing and `/episodes/{id}?embed[]=guestcast` was therefore "the only route" at 3.42M requests. The first half is true. The second half is not — the *season* episode-list route was never tested, and it honours both embeds.

| grain | requests | wall-clock at 18 req/10s |
| --- | --- | --- |
| per-episode | 3,527,180 | ~23 days |
| per-season | 188,189 | **~29 hours** |

Verified before adopting: the season response is identical to the per-episode response for both embeds, **including array order**; specials are included, both `significant_special` and `insignificant_special`, which the show episode-list route omits without `?specials=1`; and every mirrored episode carries a `season_id`, so the work list is `SELECT id FROM tvmaze.season`.

## Why this shape

**A season response is authoritative for every credit on every episode it contains.** This is the property the person axis could never have. ADR-0001 called refreshing guest cast per-episode "the single easiest thing to get wrong here", because one episode's credits belong to many people and deleting by episode would wipe other people's rows. At season grain that objection disappears: the response *is* the complete set for those episodes, so the write is a delete-and-replace of the whole season in one transaction — idempotent, and correct when a credit is removed upstream.

**Episode crew has no person-side route at all.** `/people/{id}?embed[]=guestcrewcredits` returns 400 Invalid embed type; the only person-side crew embed is `crewcredits`, which is show-level. So the person axis cannot reach episode crew at any price, and a person page sourced from it can never show "Director — S1E3". Deriving credits from the season side is the only way that capability exists.

**Guest cast comes along for free and arrives correctly ordered.** It is in the same response, so it costs zero extra requests. The person axis wrote `sort_order` as the index of a credit within *that person's own* credit list — a number that ordered an episode's guest cast by how many other guest gigs each actor had. `list_episode_guest_cast` then ordered by that column while its docstring claimed "upstream billing order". The season response carries the episode's real credit sequence, so array index is the honest value.

## Consequences

**The person axis is no longer an ingest axis.** `person_initial` is retired: `upsert_persons` writes the full attribute set from whatever payload it is handed, and show cast/crew and season guest cast/crew embeds all carry person objects byte-identical to `/people/{id}`. The only people it uniquely reached were those with no credit anywhere, whose pages render empty. The prod run was cancelled at 26% (126,706 of 486,790) on 2026-08-04 and the code deleted in NEU-962, taking `person.credits_synced_at` — the pass's resumability watermark, read by nothing else — with it. The `ck_ingest_run_kind` constraint no longer admits the kind, and is re-added `NOT VALID` so the cancelled run row stays readable. What survives is `person_update` as a pure attribute refresh — dropping `embed[]=guestcastcredits`, writing no credits, and scoped to people we already hold. It is still load-bearing and cannot be dropped: a person is not a child of a show, so a rename, a new headshot or a newly-added deathday moves no show record and reaches us by no other route. Measured at 1,118 people/day, ~10 minutes.

**Ownership inverts.** ADR-0001: "a person's credits are only ever written by the person axis." Now every credit table is written by the show axis — `show_cast` and `show_crew` from the show fetch, `episode_guest_cast` and `episode_crew` from the season fetch. Person rows may still be created by either path.

**The daily gains a season step.** A changed show has its seasons refetched: 307 shows/day × 2.11 seasons ≈ 650 requests, about 6 minutes. `/updates/shows` covers this — upstream documents that updating a direct or indirect child of a show marks the show updated. The season fetch writes credits only; show, season and episode rows stay owned by the show fetch, which must run first in the cycle because credits FK to `episode.id`.

**Credit tables now carry unique constraints**, reversing ADR-0001's deliberate omission — that stance existed because the person axis could not guarantee uniqueness at episode grain, and a single season-grain writer can. The keys are three-part: `(episode_id, person_id, character_id)` for guest cast and `(episode_id, person_id, role_id)` for episode crew. Two-part keys are violated by real data — across 1,043 sampled episodes, 36 had one person in more than one crew role and 17 had one character played by more than one person. That last figure is also a trap for the implementation: the existing per-person dedup key `(episode_id, character_id)` is correct where it sits but would silently drop legitimate rows if carried to season grain.

**`tvmaze.season` needs a watermark.** Absence of credit rows cannot stand in for "not yet fetched" — 22.5% of sampled episodes have no crew credits and a whole season may have none.

**Episode crew is modelled as its own thing, not as guest cast's sibling.** Upstream names it `guestcrew` for symmetry with `guestcast`, but an episode's director is not a guest. Its vocabulary is disjoint from show-level `crew_role`: `Writer`, `Director`, `Story`, `Teleplay` appear in the episode-level data and none of them appear in the 233-name show-level lookup, which holds production functions like Creator and Executive Producer. It gets its own interned lookup, `episode_crew_role`.

**A ~29-hour pass is ordinary, and that is the point.** Pass A was ~27h and pass C ~75h. Bringing episode credits into the same band is what moved this from "spike whether it is worth 23 days" to "build it". ADR-0002 still holds: none of this is fetched in a request path.
