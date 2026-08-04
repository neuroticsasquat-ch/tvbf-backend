# The `tvmaze` schema has two independent ingest axes

**Status:** amended (2026-08-04) by [ADR-0003](./0003-episode-credits-are-fetched-per-season.md)

> The premise below is wrong. `/seasons/{id}/episodes` honours `embed[]=guestcast` and `embed[]=guestcrew`; only the *show* episode-list route was tested. Episode credits cost ~29 hours at season grain, not ~23 days, and are now fetched by the show axis. The ownership rule stated here — that a person's credits are only ever written by the person axis — is reversed. Kept unedited below for the reasoning trail.

Until now `tvmaze` was the *show* mirror: one full-list feed (`/updates/shows`), one initial ingest, one daily delta, and every table reachable by walking down from a show. Adding cast and crew breaks that, because episode guest cast is not reachable from the show side at any acceptable cost — `/shows/{id}/episodes?embed[]=guestcast` returns nothing and `/episodes/{id}?embed[]=guestcast` is the only route, which is 3.42M requests (~22 days of rate-limit budget). From the person side the same data is **one request per person**, and `/updates/people` is a full-list feed with per-entity watermarks structurally identical to `/updates/shows`. So we made `person` a peer mirrored entity with its own initial ingest and its own daily delta, rather than cargo on a show backfill.

## Consequences

**Ownership is strict and the refresh grains differ.** `show_cast` and `show_crew` are owned by the show axis and refreshed `WHERE show_id = ?`. `episode_guest_cast` is owned by the person axis and refreshed `WHERE person_id = ?` — never per-episode, because that's not how the data arrives. `person` rows may be *created* by either axis, but a person's credits are only ever written by the person axis.

**The person axis deliberately discards data it receives.** `/people/{id}` can embed `castcredits` and `crewcredits` alongside `guestcastcredits` at no extra request cost, and they duplicate what the show axis already writes. We ignore them, because only the show side carries billing order (upstream documents `/shows/{id}/cast` as ordered by total appearances; person-side credits are unordered). Writing them would clobber `sort_order`.

**"The mirror is stale" is no longer a single question.** Each axis has its own cursor, its own daily delta and its own failure mode. Monitoring, admin status routes and any future health signal need a per-axis answer.

**A one-shot backfill was the alternative and was rejected on freshness.** Person attributes change independently of any show — a performer's name change moves no show record. A pass that populated 487k people and never revisited them would start decaying immediately. The delta that prevents this costs ~9 minutes of daily request budget (926 people changed in a sampled 24h), which made the peer-subsystem shape cheap enough that building the one-shot version first would have meant writing the resumable pass twice.
