# Changelog

All notable changes to this project will be documented in this file.

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


