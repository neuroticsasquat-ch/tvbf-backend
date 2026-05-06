# Changelog

All notable changes to this project will be documented in this file.

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


