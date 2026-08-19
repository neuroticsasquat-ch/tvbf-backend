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

**Spine** / **Sidecar**:
The spine is `catalog.show`, `catalog.season` and `catalog.episode` — the three tables holding catalog *content*, sole-sourced from TMDB (ADR-0012). A sidecar is a table in the `catalog` schema holding derived facts *about* spine rows rather than content of its own: `air_date_offset`, `airdate_show_state`, `rate_budget`, `ingest_run`. The line is load-bearing rather than tidy: a value derived from a non-TMDB source may live in a sidecar and may never touch the spine, which is what lets the airdate correction exist without reopening what NEU-1146 closed.
_Avoid_: main tables, metadata tables

**Oracle**:
A read-only third-party source consulted to answer one narrow question, and never mirrored. TV Maze is the airdate oracle: we ask it about a show's episode dates, keep one integer per `(show, season)` plus one cached show id per show, and store nothing else it returns. An oracle is not an upstream — no feed, no watermark, no delta, no pass, no rows in the spine — but it does share the rate budget, keyed by source like any other.
_Avoid_: source, upstream, provider, second catalog

**Airdate offset**:
How many days a season's mirrored airdates are shifted from TMDB's, so `air_date` means the date a US Eastern viewer saw. One integer per `(show, season)`, established against the oracle by a nightly pass that refuses whenever the evidence is not unanimous — a refusal being the absence of a verdict, not a verdict of zero. `tmdb_air_date` holds the uncorrected upstream value and is NULL wherever no correction was applied.
_Avoid_: timezone fix, date correction, airdate patch

**Work list** / **Due**:
A pass's work list is the set of rows it *may* act on — for the airdate pass, every show a user tracks or that holds a future-dated episode. Being in the work list is not being **due**: a show is due on a given night only if something could have changed for it (never reconciled, TMDB touched it since, it is still airing) or its sweep turn has come. Scope answers "is this show ours to correct", due answers "is tonight the night", and keeping them separate is what stops the run's cost tracking the catalog or the user base.
_Avoid_: queue, backlog, batch

**Sweep**:
The periodic re-check of rows nothing has flagged as changed — the backstop that catches drift on the *oracle's* side, which no watermark of ours can see. Amortised rather than periodic-in-bulk: each show belongs to a fixed bucket by id and one bucket comes due per night, so every row is swept on cadence and no night ever spikes. A sweep is what preserves a pass's self-healing property once that pass stops re-deriving everything nightly.
_Avoid_: full scan, refresh cycle, reindex

### People and credits

**Person**:
A real human in the catalog — actor, director, producer. Reached only through credits: upstream returns a person inline on every cast, crew and guest credit, so there is no person fetch of its own (ADR-0003).

A person carries a surrogate id like any other catalog row, but is the one whose id is **not carried over from TV Maze** — user data references no person, so there was nothing to line up and credits are re-ingested wholesale. `/people/{id}` URLs therefore change at cutover.
_Avoid_: actor, talent, contributor

**Character**:
A fictional or self-portrayed role, **scoped to one show** and identified by `(show_id, name)`. A character is not owned by one person: two people can be credited as the same character, and one person can play many.

The scope narrowed from global when the source changed (ADR-0007). TMDB models character as free text on a credit, so we intern per show — the same pattern as crew role. Measured cost in prod: of 1,509,298 characters, 2,621 were played by more than one person (all preserved by per-show interning) and exactly **one** spanned more than one show.
_Avoid_: role, part

**Credit**:
The umbrella term for a link between a person and something they worked on. Never used bare where the kind matters — say cast credit, crew credit, guest credit or episode crew credit.

**Cast credit**:
A person portraying a character on a show. Person-as-character.
_Avoid_: role, appearance, starring

**Crew credit**:
A person performing a named function on a show, such as Executive Producer or Editor. Person-in-function.

**Crew role**:
The named function someone performs, as a **department and a job together** — Directing/Director, Writing/Writer, Sound/Original Music Composer. Upstream sends both as free text; we intern the pair into a local lookup. One lookup covers show crew and episode crew alike.

The scope widened with the source (ADR-0007). TV Maze had two disjoint vocabularies and therefore two lookups — 233 production-function names on the show side against director, writer, story and teleplay on the episode side. TMDB emits the same department/job pair at both grains, and measured, **every episode-level pair also appears at show level**, so a second lookup would hold a copy of the first. The `tvmaze` mirror keeps its two tables until cutover.
_Avoid_: crew type, job title, episode crew role, guest crew type

**Guest credit**:
A person portraying a character in a single episode rather than across a show. Arrives on the season payload alongside the episode crew credits for the same episodes — which under TMDB is the show request itself, so both cost no request of their own (NEU-1040).
_Avoid_: guest star, one-off

**Episode crew credit**:
A person performing a named function on a single episode. Distinct from a crew credit by **grain, not by vocabulary** — the same crew role can be held at either, and one lookup serves both. TV Maze called this "guest crew" for symmetry with guest cast, but an episode's director is not a guest.
_Avoid_: guest crew, episode guest crew

**Billing order**:
The order upstream returns a show's cast in. Preserved rather than re-sorted. A property of show cast only — **crew has no order at all** under TMDB, at either grain.

It stopped being the proxy for how much someone appears when the source changed (ADR-0007): TMDB gives a credit its own **episode count**, which is the real measure and the one credits are sorted by. Billing order still says who is top-billed, which an episode count does not.
_Avoid_: cast order, importance, prominence

**Credit order**:
The sequence upstream lists a single episode's guest cast in. Preserved rather than re-sorted. Distinct from billing order: it is the episode's own credit sequence, and it says nothing meaningful when compared across episodes.
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
An episode in **season 0** — a Christmas episode, a retrospective, a DVD extra. It carries a real episode number like any other; season 0 is what marks it.

Specials are **excluded from completion math, aired counts and Watch Next**, which is upstream's own convention: a show's episode and season counts exclude season 0. A user is not held short of "finished" by a DVD extra.

The definition changed with the source (ADR-0007). TV Maze marked a special with a *null episode number* inside its real season and returned it outside the episode embed, so it needed its own fetch; TMDB parks specials in season 0 and returns them in that season's payload like anything else.
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

**Verified user**:
A user whose `email_verified_at` is not NULL. The column is **monotone** — nothing ever clears it, and an email change re-stamps it on confirm rather than revoking it — so verification is earned once and cannot be lost mid-flight. Since NEU-1161 it is also the price of social access (below).
_Avoid_: confirmed, activated, validated

**Outreach** / **consumption**:
The two halves of the social layer, and the line the verification gate is drawn on (NEU-1152, NEU-1161). **Outreach** is reaching a user who has not consented — sending a connection request, being discoverable in `/users/search` — and requires a verified email. **Consumption** is reading what an accepted connection already agreed to share — a friend's library, friend engagement on a show — and requires only the connection. Defensive acts (blocking) and withdrawal (declining, cancelling, disconnecting) are neither, and are never gated: an unverified user must always be able to protect themselves and to say yes to someone who asked.
_Avoid_: social actions (it collapses the distinction the gate rests on)

### Recommendations

**Recommendation set**:
One generated batch of suggestions for one user, written whole by a single run of the weekly pass. A set is **superseded, never mutated**: a new run inserts a new set and its rows, and the previous set simply stops being the newest. Reads take the newest set whose `status` is `succeeded`, so a run that fails or resolves nothing leaves last week's suggestions standing rather than blanking the surface.

A set is also the per-user run record — it carries the timing, the token counts, the compiled payload and the raw response, which is why the pass has no run table of its own. Its `status` distinguishes four outcomes that look identical from outside: `succeeded`, `failed`, `no_matches` (ran, resolved nothing) and `insufficient_history` (too little to generate from). Only the first is ever read.
_Avoid_: cache, prediction, batch

**Taste signal**:
A show's **LIKED / NOT LIKED / INTERESTED** classification for one user. Derived from that user's rating, completion and My Shows membership, **in that order** — a rating overrides behaviour, completion overrides membership, and the middle of the star range deliberately overrides nothing.

It is a label, not a magnitude: there is no ranking layer downstream that a number could feed, and the three values are what the model is told. INTERESTED exists because My Shows is a watchlist as much as a library, so membership alone cannot mean "liked".
_Avoid_: score, weight

**Taste payload**:
The compiled JSON describing one user's watch behaviour, sent to the model as its entire input. **Columnar** — one header naming the fields, then rows grouped by taste signal — so the label and the field names are paid for once rather than once per show. One object doing **three jobs**: it is the model's input, it is the input to the regeneration hash, and it is the exclusion list. Most of its rows name a show the user already has a record for; its `exclude` group does not have to, because a **dismissal** can name a show the user has never seen.

Being the model's *entire* input is what makes the regeneration gate provably rather than approximately correct: identical bytes mean identical output. The exclusion job is the loosest of the three — the payload caps its INTERESTED rows, so it is what the prompt is told rather than the guarantee, and a post-resolution filter is what actually enforces the rule.
_Avoid_: prompt, profile, feature vector

**Never-recommend set**:
Every show one user must never be recommended: the four project-spec §8 sources they have a record for — My Shows membership, a show rating, any episode watch, any episode rating — plus every show they have **dismissed**.

Defined **once**, in `recommendations/exclusion.py`, and enforced at both ends: the weekly pass bans them in the payload it sends and filters them out of what comes back, and `GET /me/recommendations` suppresses them at read time as a live join. Two expressions of one sentence, one in Python and one in SQL, is exactly the drift that module exists to prevent — so a sixth source goes there and nowhere else, and a client never re-implements the rule.
_Avoid_: blocklist, exclusion list

**Dismissal**:
A user removing one show from their recommendations. An *exclusion*, deliberately **not** a taste signal: it never reaches `taste_for_user`, never lands in `not_liked`, and the model is never told the user disliked anything — `not_liked` is something the model generalises from, while a dismissal is a statement about one row. Dismiss three prestige dramas you have already seen elsewhere and a taste-signal implementation teaches the model to stop recommending prestige drama.

The show need not have been recommended: one found by search is dismissible, which is why this is the one source of the never-recommend set that is not a record of having seen anything. Permanent and, today, not reversible.
_Avoid_: not interested, negative rating, thumbs down

**Resolution**:
Turning a model-authored `title` + `release_year` into a `catalog.show` surrogate id. Entirely local — fold-exact on the show name within ±1 year, then the same against its AKAs, then drop — so no upstream call is involved at any point (ADR-0002), and no fuzzy threshold either.

A resolution failure is an outcome, not a defect: an unresolved title is either a hallucination or a genuine catalog gap, and both are logged rather than mapped onto whatever scored closest. An *ambiguity*, though, resolves to the more popular row rather than to nothing — the reverse of NEU-1043, deliberately, because there a wrong pick silently misattached real watch history and here it shows a less-likely card.

That contrast is why this is **not** called matching: in this codebase matching means NEU-1043's `match_method`, a different problem with the opposite cost asymmetry.
_Avoid_: matching, lookup
