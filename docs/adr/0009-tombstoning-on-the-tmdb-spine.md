# Tombstoning on the TMDB spine

**Status:** accepted (2026-08-10)
**Amends:** [ADR-0005](./0005-shows-deleted-upstream-are-tombstoned.md)

ADR-0005's decision survives the source change: a series absent from the authoritative full list is **marked, never removed**. Every reason it gave still holds — `app.user_show_watch` and `app.user_show_rating` cascade from the show row, `app.user_episode_watch` cascades through episode, `import_ne.show_resolution` references it with NO ACTION and 522 rows, and `app.activity_event` is polymorphic with no foreign key at all, so it orphans in silence.

What does not survive is one of ADR-0005's supporting arguments and one of its two guards. This ADR amends those and leaves the rest standing.

## The reverse diff is no longer free

ADR-0005: *"`run_update` already fetches it in full to compute its cursor — so the reverse diff costs **zero extra requests**."*

That is now false. TMDB's delta is `/tv/changes`, which reports changes and cannot report a deletion: a removed series simply stops appearing, which is indistinguishable from one that did not change. The authoritative full list is a separate artifact — the daily id export, a static gzipped file (`tmdb/export.py`).

It is still nearly free, for a different reason: the export is served from `files.tmdb.org` rather than the API, so it spends no rate budget and carries no credential. One 4.7 MB download per day is the whole price. So the pass keeps ADR-0005's cadence and position — at the end of a delta that completed normally, every abort path having returned early — and pays a download for it.

**The consequence is that it must be best-effort.** A second download can fail on its own where an already-in-hand feed could not. Failing the run over it would hold `last_update_cursor` back and re-cover the whole window the next night, and the night after — the ever-widening gap NEU-1006 exists to avoid — in exchange for a reconciliation that could simply happen tomorrow. So the pass is caught and logged at ERROR, and the delta finalises on the work it actually did.

## A file can arrive half-written, and an endpoint cannot

A new failure mode, and the reason `TruncatedExportError` exists. A partial download that was decompressed leniently would yield a **valid but silently short** JSONL stream — and a short export is exactly what the reverse diff reads as proof that the missing series are gone.

Two checks, before anything reaches the diff: the body is compared against the `Content-Length` the host declared, and the gzip member is decompressed whole, so its trailing CRC32 and length refuse a stream that stops early.

## The relative floor changes denominator

ADR-0005's floors were an absolute one and *"a relative one (95% of the mirrored count)"*. The absolute floor is recalibrated proportionally, 50,000 → **150,000** against a catalog of 228,611.

The relative floor cannot be ported as written, and the reason is specific to this migration. TV Maze's mirror was the same size as its feed, so "95% of mirrored" was "95% of the feed" in all but name. Here the mirror is far smaller than the export for the whole pre-cutover period — ~63k mapped rows against ~229k series — and 95% of that is no floor at all: an export carrying two thirds of TMDB would clear both guards and tombstone every mapped series in the missing third.

So the denominator is **the larger of the mirror and the export's last measured size** (228,611, 2026-08-07). Before cutover the constant binds; after it, the mirror overtakes the constant and the guard tracks reality rather than a number measured in 2026.

ADR-0005's rule about what a tripped floor does is unchanged and load-bearing: **nothing is written at all, including resurrections**, because a feed we don't trust to prove absence cannot be trusted to prove presence.

## Locally-authored rows are exempt, and this is new

`catalog` holds rows the export has never listed and never will: the TV Maze shows and specials NEU-1042 copied in, which carry `tmdb_id IS NULL` permanently and exist to hold watch history TMDB cannot supply. A straight `mirrored - feed` flags every one of them on the first pass.

They are excluded from the diff **and** from the plausibility count — they are not evidence either way about how complete the export is. Tombstoning the rows whose entire purpose is to protect user data would be the sharpest possible version of the mistake this ADR exists to prevent.

## The diff key is `tmdb_id`, not the primary key

`catalog.show.id` is an internal surrogate that `app` references and the migration seeded from TV Maze's ids (ADR-0008). It means nothing to TMDB. Diffing on it would tombstone by coincidence — a row survives only if its TV Maze id happens to collide with a TMDB series id. The comparison is `catalog.show.tmdb_id` against the export's ids.

## Unchanged

Everything ADR-0005 decided about **what a tombstone means** ports without amendment: tombstoned is hidden from discovery rather than from its owner, tombstones lift when a series reappears, the diff is computed in Python rather than as `id NOT IN (:feed)` because 229k ids exceed Postgres's 32,767 bind-parameter cap, and the pass runs only after a loop that completed.
