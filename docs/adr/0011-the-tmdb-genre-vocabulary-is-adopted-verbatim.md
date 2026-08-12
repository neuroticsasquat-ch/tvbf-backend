# The TMDB genre vocabulary is adopted verbatim

**Status:** accepted (2026-08-12)
**Context:** [ADR-0007](./0007-tmdb-replaces-tvmaze-as-the-catalog-source.md), [ADR-0008](./0008-user-data-references-internal-ids.md), NEU-1064

The browse `genre` filter is an exact match on a name, and **TMDB's genre vocabulary is not a superset of TV Maze's**. At cutover `GET /genres` returns a different list and `?genre=Anime` starts returning nothing. The coverage audit (NEU-1031) decided this shape of question for language (D1) and status (D3); genre never came up, so it is decided here.

**TMDB's names are stored and served verbatim. Ours are not mapped onto them.** `src/tvbf/catalog/genres.py` holds the four queries that read them and the reasoning in short form.

## The measurement

`GET /genre/tv/list` against our mirror's 28, read 2026-08-09. TMDB returns 16.

| | |
|---|---|
| **In both (7)** | Animation, Comedy, Crime, Drama, Family, Mystery, Western |
| **Ours only (21)** | Action, Adult, Adventure, Anime, Children, DIY, Espionage, Fantasy, Food, History, Horror, Legal, Medical, Music, Nature, Romance, Science-Fiction, Sports, Supernatural, Thriller, Travel |
| **Theirs only (9)** | Action & Adventure, Documentary, Kids, News, Reality, Sci-Fi & Fantasy, Soap, Talk, War & Politics |

TMDB's list is coarser where TV Maze's is specific — `Science-Fiction` and `Fantasy` both land as `Sci-Fi & Fantasy`, `Action` and `Adventure` as `Action & Adventure`, `Anime` disappears into `Animation` — and carries format categories TV Maze has no equivalent for (`News`, `Reality`, `Soap`, `Talk`).

## Why not map

A mapping is the tempting answer, because it looks like it preserves what users see. It does not, and it is the expensive option:

- **It is maintained forever, against a list we do not control.** TMDB adds or renames a genre and the table is silently wrong until someone notices; every new genre needs a decision before the ingest can write it.
- **It cannot be honest in the direction that matters.** `Sci-Fi & Fantasy` → `Science-Fiction` invents a claim TMDB never made about the show; the split is one-to-many and there is no signal in the payload to split it on. The reverse (`Anime` → `Animation`) is lossy but at least true, and it is what verbatim already gives.
- **It puts a name in the response that no row holds.** The filter would then have to translate back on the way in, so the vocabulary exists in two places, only one of which the ingest keeps writing.
- **It reintroduces exactly what ADR-0007 retired.** TV Maze's vocabulary would outlive TV Maze as a translation layer over the new source.

The precedent is the audit's D1: the response *shape* is unchanged, the *values* are not, and the values are the source's.

## What it costs

`?genre=Anime` and twenty other names match nothing. Genre resolution drops from 28 buckets to 16, and the tags behind the largest of those names are not small: measured across the full 89,025-show mirror, `Romance` carries 12,099 shows, `Action` 5,345, `Adventure` 4,818, `Anime` 3,940, `Fantasy` 3,405 and `Science-Fiction` 2,466 — and every one of them either merges into a coarser TMDB name or vanishes. This is a real loss of filtering resolution, accepted knowingly and mitigated by nothing.

There is no data loss behind it: a genre is a browse facet, not user data. No `app` table references `catalog.genre`, so the hard constraint the migration is built around — no user loses a tracked show, a watched episode or a rating — is untouched by this decision.

## Genres come only from TMDB, so a show may carry none

`catalog.genre` is keyed on `tmdb_id`, so NEU-1042 deliberately copied no TV Maze genre rows: a copy with `tmdb_id IS NULL` could never match the row the ingest creates, and every genre would end up stored twice. `tmdb/upsert.py` writing `series.genres` is the only writer.

The consequence is that a show TMDB never matched — about 26k of them before NEU-1066's prune, and the ones a user tracks are kept by it — has an **empty** genre list rather than the one TV Maze gave it. That is ordinary rather than exceptional, and the queries treat it as such: hydration keys every requested show, so an untagged show reads as `[]` and not as a missing key, and both response builders already take a list.

## Two query details the target schema forced

`tvmaze.genre` carries `UNIQUE (name)`; `catalog.genre` carries only `UNIQUE (tmdb_id)`, because the name is TMDB's to change and the id is what an upsert conflicts on. The AND-semantics filter therefore counts distinct **names** rather than distinct genre ids — the filter's unit is the name, and under TMDB's published list the two counts are identical, but if a name ever arrived on two rows, counting ids would *exclude* the shows carrying both, which is the opposite of what was asked for.

Repeated values are also collapsed before the count: `?genre=Comedy&genre=Comedy` names one genre, so the bar is one. Comparing against the raw parameter count makes the query unsatisfiable, which is what happens today. That is a small, deliberate divergence from the `tvmaze` behaviour, in the direction of the semantics the filter documents.

`GenreOut.id` is the surrogate, not `tmdb_id` (ADR-0008), so the ids in the `GET /genres` body change at cutover along with the names. Nothing persists them: the SPA builds its picker from whatever the endpoint returns and filters by name.

## What this ADR does not do

Nothing calls `catalog/genres.py` yet — every read still goes to `tvmaze`, and **NEU-1047** is the pass that repoints browse, search, `/me` and credits to `catalog`. This ticket owns the decision and the queries; that one owns the switch.

A **persisted** filter value is the one place the frontend does not adapt on its own. `filterTypes.ts` builds the picker from `GET /genres`, but `usePersistedString` returns the raw stored string with no validation, so a user whose stored genre is `Science-Fiction` gets it restored and sees an empty list behind a filter that looks valid. That belongs to **NEU-1037**, which already owns persisted filter state — its description asserts the hook validates, which it does not, and the premise is corrected there.
