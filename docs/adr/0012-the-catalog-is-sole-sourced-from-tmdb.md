# The catalog is sole-sourced from TMDB, and locally-authored rows are retired

**Status:** accepted (2026-08-14)
**Context:** [ADR-0005](./0005-shows-deleted-upstream-are-tombstoned.md), [ADR-0007](./0007-tmdb-replaces-tvmaze-as-the-catalog-source.md), [ADR-0008](./0008-user-data-references-internal-ids.md), NEU-1146
**Supersedes:** the locally-authored-row half of ADR-0008 (its surrogate-key decision stands)
**Narrows:** ADR-0005, which still binds the delta (see *What is not reversed*)

`catalog.show`, `catalog.season` and `catalog.episode` hold **no** `tmdb_id IS NULL`
rows. Content TMDB does not carry is not held locally; it is deleted, along with the
user rows pointing at it. `src/tvbf/tmdb/orphan_retire.py` is the pass that made it
true, and the rule it enforces from here on.

## What this reverses

ADR-0008 sanctioned `tmdb_id IS NULL` as "a sanctioned, permanent mechanism — not a
migration artifact", and the TMDB migration carried an absolute constraint above it:

> No user loses a tracked show, a watched episode, or a rating. Period. This holds even
> where TMDB has no counterpart.

Both are withdrawn. Sole-sourcing wins, and the measured cost is **~95 watch records
across 5 users** — 0.024% of the grain — plus one rating.

## Why

**The licence made it a live cost, not a tidiness preference.** 782,161 episode rows,
18,341 season rows and 2 show rows still held TV Maze titles, airdates and numbering.
While they are served, CC BY-SA 4.0 attribution is a *condition of serving them* — the
reason `tvbf-frontend` kept a TVmaze credit in its footer after NEU-1049 swapped
everything else to TMDB. A permanent locally-authored residue means a permanent
attribution obligation to a source we no longer use, and a catalogue whose provenance is
"mostly TMDB" forever.

**The row count invited the wrong conclusion in both directions.** "TMDB doesn't have
this, so delete it" would have destroyed 191 watch records: 99.999% of those orphans sit
under shows that *are* matched, inside season ranges TMDB covers in full. They were
unmapped twins, not missing content. But the opposite conclusion — keep everything
unmatched forever — was equally wrong, because it treats a two-parter TMDB counts as one
episode as though it were content TMDB lacks. The measurement is what separated them:
four match tiers rescued 121,997 of 782,155 orphans, including **all 17** Will & Grace
user-touched rows and every one of the 64 rescued by a unique title.

**What is actually lost is not what the constraint imagined.** The constraint was written
against "a show TMDB does not list" — a real gap in upstream coverage. The residue is
overwhelmingly not that. It is compilations and retrospectives filed as episodes
(`SNL 40th Anniversary`, `The Best of Will Ferrell, Volume 1`), webisode runs, films
(`El Camino: A Breaking Bad Movie`, `Friends: The Reunion`), and halves of two-parters
TMDB models as single episodes. **TV Maze and TMDB disagree about what an episode is, not
about what aired.** Preserving that disagreement forever, in a database sole-sourced from
one of them, preserves an artifact of the other's editorial model.

**The loss is bounded, enumerated and recoverable.** The pre-drop `pg_dump` of
`app.watch_archive` (NEU-1158) holds a human-readable snapshot of all 9,359 watches and
ratings — show name, premiere year, season, episode number and title, air date — carries
no foreign keys, and survived everything including `DROP SCHEMA tvmaze CASCADE`. It was
built for exactly this. Every row this pass deletes is printed by `report` **before** the
pass runs, split into de-duplications a surviving twin already records and genuine losses,
and that list is
reviewed and committed as the artifact of record.

## What is not reversed

**ADR-0005 still binds the delta, and this is not the exception that swallows it.**
That ADR forbids `DELETE` on `catalog.show` — a show the *upstream feed stopped listing*
is tombstoned, because `app.user_show_watch` and `app.user_show_rating` cascade from it,
`import_ne.show_resolution` references it with NO ACTION, and `app.activity_event` has no
foreign key at all and would orphan silently. Every one of those facts is still true, and
`tmdb/tombstone.py` is unchanged.

The two are about different rows. Tombstoning protects a show TMDB **had and dropped**,
where a delete would destroy history on the strength of a feed that may be wrong tomorrow
— which is why it is floor-guarded. This pass deletes a show TMDB **never had**, after a
matcher has failed to find it a counterpart and a report has named every user row that
goes with it. The evidence is the difference: one is an absence in today's download, the
other is a reviewed conclusion. So the delete here re-asserts its own guards rather than
inheriting any — `tmdb_id IS NULL`, no catalog children, nothing left referencing it —
and a show `import_ne` still points at is skipped and reported, never cascaded over.

**The surrogate-key decision stands.** `app` still references `catalog.show.id` and
`catalog.episode.id`, never `tmdb_id`; upserts still conflict-target `tmdb_id`. That half
of ADR-0008 is what made this pass a re-point rather than a rewrite, and re-deriving ids
away from the preserved TV Maze values is explicitly out of scope.

`tmdb_id IS NULL` therefore remains *representable* and remains the thing ingest must
never overwrite — `ON CONFLICT (tmdb_id)` still cannot match a NULL, and
`prune_missing_seasons` still carries its own `tmdb_id IS NOT NULL` guard. What changes
is that no such row is allowed to **persist**: it is now a defect to be retired, not a
sanctioned resting state.

## Consequences

**A `tmdb_id IS NULL` row in the spine is now an alarm.** Deltas can create fresh ones,
so `task retire:orphans` is re-runnable and **must be re-run after any later ingest or
delta** — the same property `season_dedupe` carries. Criterion 7's query is the check:
zero rows at all three grains.

**`task reconcile:verify` does not come back clean, by design.** Three expected
discrepancy classes, all confirmed line-by-line against the report before the pass and
recorded in `docs/migration/README.md`: genuine losses, de-duplications, and *gains* —
this is the one pass that **creates** `app` rows, adding a show to a user's My Shows when
their history moves to a series they did not track. A `LOST` line not on the report's
list is a stop.

**The TVmaze attribution comes out of the frontend footer**, and only once criterion 7
holds in production. That is the visible point of the whole exercise.

**It is not reversible.** The pre-drop `tvmaze` dump is the only source for the deleted
catalog rows, and it cannot restore `app` rows at all.
