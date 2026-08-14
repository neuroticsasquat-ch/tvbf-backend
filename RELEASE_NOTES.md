# Release notes

## 0.3.0 — 2026-08-14

### Catalog

- Index the show->episode air FKs so cascaded deletes do not seq scan ([NEU-1066](https://linear.app/neuroticsasquatch/issue/NEU-1066))
- Repoint browse, search, /me and credits reads to catalog ([NEU-1047](https://linear.app/neuroticsasquatch/issue/NEU-1047))
- Backfill the credits the ingest mirrored before the writers existed ([NEU-1127](https://linear.app/neuroticsasquatch/issue/NEU-1127))
- Drop the tvmaze schema and relocate ingest_run ([NEU-1051](https://linear.app/neuroticsasquatch/issue/NEU-1051)) ([#252](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/252))
- Retire the TV Maze orphan rows from the catalog spine ([NEU-1146](https://linear.app/neuroticsasquatch/issue/NEU-1146))
- Keep specials out of the tier 2 season offset ([NEU-1146](https://linear.app/neuroticsasquatch/issue/NEU-1146))
- Separate proven from inferred de-duplications in the loss list ([NEU-1146](https://linear.app/neuroticsasquatch/issue/NEU-1146))

### General

- Run the daily update as a CLI entrypoint on a Coolify schedule ([NEU-1008](https://linear.app/neuroticsasquatch/issue/NEU-1008))
- Create the catalog schema and wire it through migrations, db:init and tests ([NEU-1026](https://linear.app/neuroticsasquatch/issue/NEU-1026))
- Create the catalog schema in CI too, the fifth hand-enumerated site ([NEU-1026](https://linear.app/neuroticsasquatch/issue/NEU-1026))
- Key request budgets by source and lease blocks ([NEU-1027](https://linear.app/neuroticsasquatch/issue/NEU-1027))
- Add the TMDB API client with bearer auth and a measured append cap ([NEU-1028](https://linear.app/neuroticsasquatch/issue/NEU-1028))
- Snapshot every watch and rating into app.watch_archive ([NEU-1029](https://linear.app/neuroticsasquatch/issue/NEU-1029))
- Add the per-user, per-show reconciliation harness ([NEU-1030](https://linear.app/neuroticsasquatch/issue/NEU-1030))
- Add catalog table definitions for the full TMDB surface ([NEU-1032](https://linear.app/neuroticsasquatch/issue/NEU-1032))
- Parse TMDB payloads and upsert them into catalog ([NEU-1033](https://linear.app/neuroticsasquatch/issue/NEU-1033))
- Copy tvmaze into catalog with ids preserved ([NEU-1042](https://linear.app/neuroticsasquatch/issue/NEU-1042))
- Map catalog rows onto tmdb_id in three tiers ([NEU-1043](https://linear.app/neuroticsasquatch/issue/NEU-1043))
- Mirror TMDB's whole catalog from the daily id export ([NEU-1034](https://linear.app/neuroticsasquatch/issue/NEU-1034))
- Size the catalog pass from measurement and split its failure counts ([NEU-1034](https://linear.app/neuroticsasquatch/issue/NEU-1034))
- Keep the catalog current with a daily /tv/changes delta ([NEU-1035](https://linear.app/neuroticsasquatch/issue/NEU-1035))
- Tombstone series absent from the TMDB id export ([NEU-1036](https://linear.app/neuroticsasquatch/issue/NEU-1036))
- Add catalog credit tables for person, character, cast and crew ([NEU-1038](https://linear.app/neuroticsasquatch/issue/NEU-1038))
- Apply pre-ship review findings on the catalog credit tables ([NEU-1038](https://linear.app/neuroticsasquatch/issue/NEU-1038))
- Apply pre-ship review findings on the episode credit writer ([NEU-1040](https://linear.app/neuroticsasquatch/issue/NEU-1040))
- Apply pre-ship review findings on the human matching queue ([NEU-1044](https://linear.app/neuroticsasquatch/issue/NEU-1044))
- Apply pre-ship review findings on the episode-grain mapping ([NEU-1045](https://linear.app/neuroticsasquatch/issue/NEU-1045))
- Compose TMDB image URLs in the API response ([NEU-1063](https://linear.app/neuroticsasquatch/issue/NEU-1063))
- Adopt TMDB's genre vocabulary verbatim at cutover ([NEU-1064](https://linear.app/neuroticsasquatch/issue/NEU-1064))
- Close the review gaps in the genre queries ([NEU-1064](https://linear.app/neuroticsasquatch/issue/NEU-1064))
- Add season_name to the friends feed payload ([NEU-1132](https://linear.app/neuroticsasquatch/issue/NEU-1132))
- Exclude special episodes from watch progress ([NEU-1062](https://linear.app/neuroticsasquatch/issue/NEU-1062))
- Un-marking a season must not skip copied specials ([NEU-1062](https://linear.app/neuroticsasquatch/issue/NEU-1062))
- Keep a special's feed event when its season or show is bulk-marked ([NEU-1062](https://linear.app/neuroticsasquatch/issue/NEU-1062))
- Correct mirrored airdates with a per-season offset ([NEU-1145](https://linear.app/neuroticsasquatch/issue/NEU-1145))
- Read the TV Maze lookup's id from its 301 Location header ([NEU-1145](https://linear.app/neuroticsasquatch/issue/NEU-1145))
- Cache the TV Maze show id so the airdate pass stops re-resolving it ([NEU-1148](https://linear.app/neuroticsasquatch/issue/NEU-1148))
- Correct the cached link's counters and its two dead-end paths ([NEU-1148](https://linear.app/neuroticsasquatch/issue/NEU-1148))
- Scope the nightly airdate work list to change ([NEU-1149](https://linear.app/neuroticsasquatch/issue/NEU-1149))

### Migration

- Add the human matching queue for unresolved user-touched shows ([NEU-1044](https://linear.app/neuroticsasquatch/issue/NEU-1044))
- Map the copied episode rows onto TMDB ids ([NEU-1045](https://linear.app/neuroticsasquatch/issue/NEU-1045))
- Deduplicate the season grain against the TMDB ingest ([NEU-1119](https://linear.app/neuroticsasquatch/issue/NEU-1119))
- Prune the unmatched copied shows the ingest duplicated ([NEU-1066](https://linear.app/neuroticsasquatch/issue/NEU-1066))
- Add the pre-cutover catalog coverage go/no-go gate ([NEU-1048](https://linear.app/neuroticsasquatch/issue/NEU-1048))
- Close the gate on unconfirmed tier-3 guesses ([NEU-1048](https://linear.app/neuroticsasquatch/issue/NEU-1048))
- Repoint the app foreign keys onto catalog ([NEU-1046](https://linear.app/neuroticsasquatch/issue/NEU-1046))
- Re-point user history onto the ingested episode rows ([NEU-1126](https://linear.app/neuroticsasquatch/issue/NEU-1126))

### Tmdb

- Mirror show cast and crew from aggregate_credits ([NEU-1039](https://linear.app/neuroticsasquatch/issue/NEU-1039))
- Mirror episode guest cast and crew from the season payload ([NEU-1040](https://linear.app/neuroticsasquatch/issue/NEU-1040))
- An episode credit with no person no longer fails the series payload ([NEU-1128](https://linear.app/neuroticsasquatch/issue/NEU-1128))

## 0.2.2 — 2026-08-07

### Scripts

- Band crew volume on tracked shows, not the whole catalogue ([#200](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/200))

### Tvmaze

- Tombstone shows deleted upstream ([NEU-1005](https://linear.app/neuroticsasquatch/issue/NEU-1005)) ([#201](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/201))
- Gone entities must not trip the consecutive-failure abort ([NEU-1006](https://linear.app/neuroticsasquatch/issue/NEU-1006)) ([#202](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/202))

## 0.2.1 — 2026-08-06

### Admin

- Reject concurrent runs of the same kind ([NEU-966](https://linear.app/neuroticsasquatch/issue/NEU-966)) ([#189](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/189))

### Browse

- Expose episode crew on the browse API ([NEU-963](https://linear.app/neuroticsasquatch/issue/NEU-963)) ([#193](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/193))

### Scripts

- Rate-base the episode-credits failure band and widen it

### Tvmaze

- Add episode credit table definitions and season watermark ([NEU-959](https://linear.app/neuroticsasquatch/issue/NEU-959)) ([#190](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/190))
- Fetch episode credits per season and cut over ownership ([NEU-960](https://linear.app/neuroticsasquatch/issue/NEU-960)) ([#191](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/191))
- Season credit backfill pass + admin routes ([NEU-961](https://linear.app/neuroticsasquatch/issue/NEU-961)) ([#194](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/194))
- Prune seasons deleted upstream from the mirror ([NEU-967](https://linear.app/neuroticsasquatch/issue/NEU-967)) ([#197](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/197))

## 0.2.0 — 2026-08-04

### Browse

- Disable browser cache on user-mutable endpoints
- Add show cast and crew routes ([NEU-940](https://linear.app/neuroticsasquatch/issue/NEU-940)) ([#174](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/174))
- Add person detail and credits routes ([NEU-948](https://linear.app/neuroticsasquatch/issue/NEU-948)) ([#175](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/175))
- Add person search and folded person-name index ([NEU-950](https://linear.app/neuroticsasquatch/issue/NEU-950)) ([#176](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/176))
- Add episode guest-cast route ([NEU-949](https://linear.app/neuroticsasquatch/issue/NEU-949)) ([#177](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/177))

### Feedback

- Wire LinearClient settings, lifespan, and dependency
- Add POST /me/feedback route forwarding to Linear
- Send maintainer notification email after Linear submit

### General

- Scope ingest cursor lookup per axis ([NEU-954](https://linear.app/neuroticsasquatch/issue/NEU-954))
- Add credit table definitions and migration ([NEU-936](https://linear.app/neuroticsasquatch/issue/NEU-936)) ([#168](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/168))

### Integrations

- Add Linear GraphQL client for feedback flow
- Use externalId (singular) in customerUpsert

### Observability

- Auto-instrument the API with OpenTelemetry → SigNoz

### Scripts

- Restore all cross-schema FKs on db:refresh ([#180](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/180))
- Add pass A verification script ([NEU-938](https://linear.app/neuroticsasquatch/issue/NEU-938)) ([#181](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/181))
- Retune pass A verification bands to measured totals ([#182](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/182))

### Search

- Fold accents and punctuation in show name search ([NEU-433](https://linear.app/neuroticsasquatch/issue/NEU-433))

### Tvmaze

- Honour show embed and add cast/crew upserts ([NEU-937](https://linear.app/neuroticsasquatch/issue/NEU-937)) ([#169](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/169))
- Alias externals thetvdb so TVDB ids stop parsing as None ([NEU-922](https://linear.app/neuroticsasquatch/issue/NEU-922)) ([#170](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/170))
- Fetch specials on ingest and daily delta ([NEU-933](https://linear.app/neuroticsasquatch/issue/NEU-933)) ([#171](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/171))
- Add pass A show-refresh orchestrator ([NEU-926](https://linear.app/neuroticsasquatch/issue/NEU-926)) ([#172](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/172))
- Add pass C person ingest and guest cast ([NEU-942](https://linear.app/neuroticsasquatch/issue/NEU-942)) ([#178](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/178))
- Add daily person delta ([NEU-943](https://linear.app/neuroticsasquatch/issue/NEU-943)) ([#179](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/179))
- Make the rate limiter process-wide, not per client ([NEU-955](https://linear.app/neuroticsasquatch/issue/NEU-955)) ([#184](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/184))
- Warn when a second rate-limiter budget is created ([NEU-957](https://linear.app/neuroticsasquatch/issue/NEU-957)) ([#185](https://github.com/neuroticsasquat-ch/music-discovery-engine/pull/185))

# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0] - 2026-05-16

### Bug Fixes

- Apply email_change enum value on alembic's own connection (#83)

### Features

- Add cookie-auth invite creation with email delivery and admin-scoped invite list (NEU-187) (#98)
- Add is_admin column, require_admin_user dep, and admin user-management routes (NEU-185) (#97)
- Add privacy columns, PATCH toggles, and feed filtering (NEU-180) (#96)
- Add GET /me/feed with cursor pagination and read-time rollup (NEU-178) (#95)
- Emit/cancel activity events from /me mutation services (NEU-174) (#94)
- Add activity_event table and emit/cancel service (NEU-173) (#93)
- Surface my_rating in browse and /me/shows; tighten browse cache (#92)
- Add friend ratings endpoints (#91)
- Add ratings backfill orchestrator and admin endpoints (#90)
- Surface tvmaze rating_average through ingest and browse (#89)
- Add user-rating tables and CRUD endpoints (#88)
- Add GET /me/export streaming JSON endpoint (NEU-158) (#87)
- Add session-revocation endpoints (NEU-155) (#86)
- Add GET /me/sessions + debounce touch (NEU-152) (#85)
- Add PATCH /me for display_name updates (NEU-149) (#84)
- Add forgot-password + reset-password endpoints (NEU-146) (#82)
- Add email-change request + confirm endpoints (NEU-143) (#81)
- Add email-verification endpoints + signup auto-send (NEU-140) (#80)
- Wire Resend + SMTP email provider module (#78)
- Add auth_token table, repo, and service (#77)

### Refactor

- Make clients explicitly inherit from EmailClient protocol (#79)

## [2026-05-10] - 2026-05-10

### Bug Fixes

- Show only the next unaired season per show (#73)
- Set Cache-Control: private on browse routes (NEU-93) (#70)
- Include watched flag on /me list episodes (NEU-100) (#64)
- Accept client-supplied today for watch-next/upcoming bucketing (#48)

### Features

- Add /me/upcoming/{seasons,shows} endpoints (NEU-135) (#72)
- Add first_watched_at to MyShowEntry (NEU-122) (#71)
- Expand Watched sort options to match Active (NEU-114) (#69)
- Add friend engagement endpoints (NEU-111) (#68)
- Add friend library endpoints and connection check (NEU-108) (#67)
- Add /me/watched endpoint (NEU-102) (#66)
- Add block/unblock endpoints + cross-cutting filtering (NEU-78) (#58)
- Add list connections + remove endpoint (NEU-77) (#57)
- Add list/accept/reject connection-request endpoints (NEU-76) (#56)
- Add POST /connection-requests endpoint (NEU-75) (#55)
- Add /users/search endpoint with block filtering (NEU-74) (#54)
- Add connection repo, service, and schemas (NEU-73) (#53)
- Add app.connection model and migration (NEU-72) (#52)
- Redefine airdate_desc as last-aired, add unwatched_airdate_desc (#44)

### Refactor

- Rename dto modules to schemas (FastAPI convention) (#49)

## [2026-05-06] - 2026-05-06

### Bug Fixes

- Accept client-supplied today for watch-next/upcoming bucketing (#48)

### Refactor

- Rename dto modules to schemas (FastAPI convention) (#49)

## [2026-05-05] - 2026-05-05

### Features

- Redefine airdate_desc as last-aired, add unwatched_airdate_desc (#44)

## [2026-05-04] - 2026-05-05

### Features

- Include matched_aka in search responses (#41)

## [2026-05-03] - 2026-05-03

### Features

- Match shows by TVMaze AKA titles (#38)
- Add show-level bulk watch and per-season progress endpoints (#37)
- Add last_aired sort key to /shows (#36)
- Expose aired counts, last-watched, and added_at on list entries (#35)
- Add aired and upcoming episode counts per entry (#34)
- Expose last_watched_at and last_aired per entry (#28)


