# Release notes

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


