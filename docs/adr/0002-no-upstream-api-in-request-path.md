# No upstream API call ever sits in a live request path

**Status:** accepted (2026-08-01)

Everything a user request reads must already be in Postgres. When we need data from an upstream API — TV Maze today, TMDB later — we mirror it ahead of time via a pass and keep it current with a daily delta. We do not fetch it lazily on a cache miss, even behind a TTL.

This came up sharply while designing Cast and Crew. Fetch-on-demand was genuinely attractive there: a complete person page is 3 upstream requests, an episode's guest cast is 1, and mirroring the person axis instead costs ~75 hours of rate-limited budget to populate 487k people whose pages mostly nobody will open. We mirrored anyway.

## Why

**A cache miss is still an outbound call, and load scales with our traffic, not our catalog.** A TTL reduces the multiplier; it doesn't change the shape. If the app gets popular, that load is transferred to a free community API that never agreed to serve it. Mirroring caps our upstream cost at a function of catalog size and change rate — quantities we control and can reason about — rather than user behaviour, which we can't.

**Correctness guarantees need a delta, and a delta makes the cache redundant.** A person's name can change; the canonical case is Elliott Page. A TTL'd cache propagates that only for pages someone happens to open after expiry, which is not a guarantee. Once you build the `/updates/people`-driven delta that *is* a guarantee, you already have the mechanism that keeps a full mirror correct — and the cache is now a second, weaker copy of the same data.

**It keeps upstream out of our failure and latency budget.** No spinner on a cold page, no timeout policy in a request handler, no user-facing request contending with the ingest jobs for the same rate limiter.

## Consequences

Read paths are plain SQL against local tables, which is also what makes person search possible at all — you cannot index what you haven't fetched.

The cost is paid in unrepeatable, rate-limited wall-clock: ~102 hours across two passes for this feature alone. That makes table definitions expensive to get wrong and makes "freeze the shape before the pass runs" a real sequencing constraint rather than good hygiene.

This applies to data. Images are the current exception — show and person artwork is still hot-linked to the TV Maze CDN. Mirroring image bytes locally is wanted for the same reason and is simply not done yet.
