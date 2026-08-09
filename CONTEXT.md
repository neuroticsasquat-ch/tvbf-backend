# TV Binge Friend

A web app for tracking TV watching with a social layer, built on a local mirror of an upstream TV catalog. This glossary fixes the vocabulary used across `tvbf-backend`, `tvbf-frontend`, specs, plans and tickets.

**The upstream source is changing.** TMDB replaces TV Maze as the catalog source (ADR-0007), and the mirror moves from the `tvmaze` schema to a source-neutral `catalog` schema. Entries below marked _(transitional)_ describe vocabulary that is TV Maze-specific and retires with it.

## Language

### The catalog

**Catalog mirror**:
Our local copy of the upstream catalog. Authoritative for reads; never written to by users. Deliberately not named for its source — that has changed once already.
_Avoid_: cache, snapshot

**Surrogate id**:
The internal, generated primary key of a catalog row (`catalog.show.id`, `catalog.episode.id`). Everything user-facing and everything in `app` references this, never the upstream key (ADR-0008). The upstream id lives alongside as a unique natural key.
_Avoid_: internal id, local id, pk

**Locally-authored row**:
A catalog row with no upstream counterpart — `tmdb_id IS NULL`. The sanctioned way to hold a show or episode a user tracks that upstream does not list. Permanent, not a migration artifact; ingest never deletes or overwrites one.
_Avoid_: orphan, manual entry, stub

**Ingest axis**:
An independently-fetched region of the catalog mirror, with its own upstream full-list feed, watermark, initial ingest and daily delta. _(transitional: the TV Maze split into show and person axes does not survive — TMDB returns credits on the show request.)_

**Pass**:
A one-time run over an entire ingest axis to populate or repair a region of the mirror. Measured in hours of rate-limited request budget, and treated as unrepeatable in practice.
_Avoid_: migration, sync, job

**Watermark**:
A per-row timestamp recording that a particular kind of data has been fetched for that row, making a pass resumable after a crash. Distinct from the run cursor a daily delta advances.

**Daily delta**:
The recurring job that re-fetches only the entities upstream reports as changed since the last run.
_Avoid_: sync, refresh

**Phantom record**:
A mirrored row for an entity upstream has deleted. Revealed by the fetch that names its parent — a phantom season by its show's payload no longer listing it, a phantom show by its absence from `/updates/shows` — never by anything about the row itself.

What happens next differs by grain, deliberately: **phantom seasons are deleted** (ADR-0004), **phantom shows are tombstoned** (ADR-0005). Deleting a season is recoverable — the next show fetch re-creates it with the same upstream id — and no user data references one. Deleting a show is not: `app.user_show_watch` and `app.user_show_rating` cascade from it, and nothing upstream knows a user was tracking it.
_Avoid_: orphan, stale row

**Tombstone**:
Marking a mirrored row as gone upstream instead of removing it — `show.deleted_upstream_at`. A tombstoned show is hidden from discovery (browse, search) but stays fully functional for users already tracking it, and is resurrected if it reappears upstream. Not a synonym for phantom: a phantom is the situation, a tombstone is one of the two responses to it.
_Avoid_: soft delete, archived, disabled

### People and credits

**Person**:
A real human in the catalog — actor, director, producer. Identified by a TV Maze person id.
_Avoid_: actor, talent, contributor

**Character**:
A fictional or self-portrayed role, **scoped to one show** and identified by `(show_id, name)`. A character is not owned by one person: two people can be credited as the same character, and one person can play many.

The scope narrowed from global when the source changed (ADR-0007). TMDB models character as free text on a credit, so we intern per show — the same pattern as crew role. Measured cost in prod: of 1,508,888 characters, 2,621 were played by more than one person (all preserved by per-show interning) and exactly **one** spanned more than one show.
_Avoid_: role, part

**Credit**:
The umbrella term for a link between a person and something they worked on. Never used bare where the kind matters — say cast credit, crew credit, guest credit or episode crew credit.

**Cast credit**:
A person portraying a character on a show. Person-as-character.
_Avoid_: role, appearance, starring

**Crew credit**:
A person performing a named function on a show, such as Executive Producer or Editor. Person-in-function.

**Crew role**:
The named function on a show-level crew credit. Upstream sends it as free text; we intern it into a local lookup.
_Avoid_: crew type, job title

**Episode crew role**:
The named function on an episode crew credit — director, writer, story, teleplay. Interned into its own lookup, kept separate from crew role because the two vocabularies share no values.
_Avoid_: guest crew type, crew role

**Guest credit**:
A person portraying a character in a single episode rather than across a show. Reachable per-season from upstream, alongside the episode crew credits for the same episodes.
_Avoid_: guest star, one-off

**Episode crew credit**:
A person performing a named function on a single episode — director, writer, story, teleplay. Distinct from a crew credit, which is a function performed across a whole show: the two vocabularies share no values. Upstream calls this "guest crew" for symmetry with guest cast, but an episode's director is not a guest.
_Avoid_: guest crew, episode guest crew

**Billing order**:
The order upstream returns a show's cast in, reflecting each character's total appearances. Preserved rather than re-sorted. A property of show cast only.
_Avoid_: cast order, importance, prominence

**Credit order**:
The sequence upstream lists a single episode's credits in — guest cast and episode crew alike. Preserved rather than re-sorted. Distinct from billing order: it is the episode's own credit sequence, not a count of appearances, and it says nothing meaningful when compared across episodes.
_Avoid_: billing order, sort order

**Filmography**:
The complete itemized set of a person's credits — cast, crew, guest and episode crew — as presented on their page.
_Avoid_: credits list, appearances

**Credit group**:
All of one person's credits of a single kind on a single show, presented as one filmography entry — "Director · 12 episodes of *Severance*" rather than twelve rows. A presentation concept only: the API returns credits individually and grouping happens client-side. Grouping never merges across kinds, so a person who both acted in and directed a show has a cast entry and an episode-crew entry for it.
_Avoid_: credit cluster, merged credit

### Shows and episodes

**Show**:
A television program, identified by a TV Maze show id. The root of the show axis.

**AKA**:
An alternate title for a show, usually a foreign-market name. Semantically *a name of the show*, which is why title search matches against it.
_Avoid_: alias, alternate name

**Special**:
An episode upstream marks with a null number — a Christmas episode, a retrospective, a DVD extra. Excluded from the episode embed, so it requires its own fetch.
_Avoid_: extra, bonus episode

### The app

**My Shows**:
The set of shows a user has explicitly added to track.
_Avoid_: watchlist, favorites, following

**Watch Next**:
The computed next unwatched episode for each show in a user's My Shows.

**Connection**:
A link between two users, in one of three states: requested, accepted, or blocked. Friend-scoped features read only accepted connections.
_Avoid_: friend, follow, relationship

**Invite code**:
A single-use code required to sign up. Never expires; consumed on use.
