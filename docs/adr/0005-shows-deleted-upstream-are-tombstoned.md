# Shows deleted upstream are tombstoned, not deleted

**Status:** accepted (2026-08-06)
**Amends:** [ADR-0004](./0004-the-show-fetch-owns-the-season-set.md)

A show absent from `/updates/shows` has been deleted by TV Maze. We mark it — `show.deleted_upstream_at` — and never remove the row.

ADR-0004 established that the mirror **deletes** phantom seasons. This ADR deliberately does the opposite one level up, and the asymmetry is the point.

## Why deletion doesn't survive contact with a show

ADR-0004's decisive argument was that **deletion is recoverable by construction**: a wrongly deleted season is re-created with the same upstream id by the next show fetch, `season.credits_synced_at` is derived state, and `episode.season_id` is re-resolved from the number map. Nothing unrecoverable is lost.

That argument does not carry over. `app.user_show_watch` and `app.user_show_rating` reference `tvmaze.show` with `ON DELETE CASCADE`, and `app.user_episode_watch` cascades through `tvmaze.episode`. A wrongly deleted `user_show_watch` row is simply gone — **nothing upstream knows the user was tracking that show**, so no fetch can restore it. Deleting a show to tidy the mirror trades user data for neatness.

Two further findings, either of which sinks a hard delete on its own:

**A hard delete cannot even run in the general case.** `import_ne.show_resolution` references `tvmaze.show` with `confdeltype = 'a'` — NO ACTION, not cascade — and holds 522 rows. A referenced show raises a foreign-key violation instead of deleting. The 58 shows removed by hand during NEU-967's cleanup succeeded only because none happened to be referenced. That was luck.

**`app.activity_event` has no foreign key at all.** It is polymorphic — `target_type` + `target_id`, with only an index — and holds 741 rows. Deleting a show leaves activity events pointing at a target that no longer exists, silently. Neither cascade nor NO ACTION applies, so nothing would catch it.

## The signal, and the guard that matters more

A show mirrored but **absent from `/updates/shows`** is gone. That feed is the authoritative full list of every show id upstream holds, and `run_update` already fetches it in full to compute its cursor — so the reverse diff costs **zero extra requests**.

Validated against prod on 2026-08-06, immediately after the manual cleanup:

```
feed entries                      88,997
mirrored shows                    88,971
in DB but NOT in feed                  0   ← exactly the 58 already removed
in feed but NOT in DB                 26   ← ordinary ingest backlog
```

Zero false positives: the diff identified precisely the population that hand-probing `/shows/{id}` had found.

**The guard matters more than the signal.** The diff is `mirrored - feed`, so a truncated or empty feed returning 200 would tombstone all 89k shows — ADR-0004's empty-embed footgun, one level up. Two floors, both required: an absolute one (50,000 entries, catching an empty or badly truncated 200) and a relative one (95% of the mirrored count, catching a partial feed large enough to clear the absolute floor). When either trips, **nothing is written at all — including resurrections**, because a feed we don't trust to prove absence cannot be trusted to prove presence.

The pass runs **after** the daily's per-show loop completes normally. Every abort path returns early, so a run that gave up partway never reconciles against a catalogue it only half saw.

**Tombstones lift.** A show that reappears in the feed has `deleted_upstream_at` cleared. TV Maze does restore ids, and a mark that could never be removed would be a one-way door — which is most of what made deletion unattractive.

## Tombstoned means hidden from discovery, not from its owner

A tombstoned show is excluded from browse and search, so nobody can newly find or add one. For users already tracking it, **My Shows, Watch Next, Upcoming, watch history and ratings keep working unchanged**, and the show detail page stays reachable by id.

Losing a show off your list with no explanation is worse than the mirror carrying a marked row. This also answers ADR-0004's objection to tombstoning — "a filter every future query has to remember" — by keeping the filter on **one** query, `list_shows`, rather than all ~28 show-facing ones.

**Person filmographies deliberately still show tombstoned shows.** `list_person_cast_credits` and its siblings join `show`, so a tombstoned show remains visible on a person's page, and reachable from there. That is intentional: a filmography is a factual record of what someone worked on, and silently dropping entries from it would misrepresent their career rather than protect anyone. The discovery surface this ADR closes is browse and search — the places a user goes to *find something to watch* — not every path by which a show id can be reached.

## Consequences

**A tombstoned show's seasons stay unstamped forever, by design.** `credits_synced_at` will never be set for them, because the show can no longer be fetched. Any coverage check must exclude tombstoned shows or it will report a permanent, unfixable residue — this is exactly the gap that made NEU-967's acceptance criterion unreachable as written, and it needed 58 hand-deletions to reach zero.

**The consecutive-failure abort is still a separate problem.** Tombstoning reduces how often deleted shows are attempted; it does not stop a run dying when a contiguous block of them is encountered before the tombstone pass runs. See NEU-1006.

**Episodes deleted upstream remain unhandled.** Same class one level down, and `app.user_episode_watch` FKs the episode directly, so it carries the identical user-data hazard. This ADR is what that sequel amends.
