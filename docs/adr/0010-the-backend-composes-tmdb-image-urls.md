# The backend composes TMDB image URLs

**Status:** accepted (2026-08-12)
**Context:** [ADR-0007](./0007-tmdb-replaces-tvmaze-as-the-catalog-source.md), NEU-1063

TV Maze stored a **full image URL**. TMDB stores a **path fragment** — `/abc.jpg` — which is only an image once a base URL and a size are prepended. Something has to compose the two, and until this ADR nothing decided what.

**The backend composes.** `image_medium` and `image_original` keep carrying finished URLs, on shows, seasons, episodes and people alike, so the API contract at cutover is byte-for-byte what it is today and the SPA needs no work at all. `src/tvbf/catalog/images.py` is the one place it happens.

## Why not ship the path

Shipping `poster_path` and a base URL is the more honest shape — it is what TMDB actually holds, and it lets each surface pick a size appropriate to how large it renders, which is a real capability the composed form gives up.

It is also a **contract change**: every image field in the response changes meaning, and every `<img src>` in the SPA changes with it. That needs a matching frontend ticket, and none exists — NEU-1047, which this ticket blocks, opens by asserting the API contract does *not* change, and composing is what makes that sentence true. The migration is already large; making the cutover visible to the client is a cost with no deadline attached to it.

The reversal cost is small and stays small: the paths are stored, the composition is one module, and the day a surface genuinely needs per-surface sizing, an *additive* `poster_path` field can be shipped alongside the composed pair rather than in place of it.

## The price is that the backend picks the sizes, so they are written down

A composed URL bakes in a size choice that the client can no longer make. That choice is therefore recorded in `KINDS` in `catalog/images.py` — every size TMDB offers per kind, and which of them each API field name resolves to — and `ImageKind` refuses a size upstream does not offer, so the record cannot drift into fiction.

| API field | Kind | TV Maze today (measured) | TMDB size | Why |
|---|---|---|---|---|
| `image_medium` | poster (show, season) | 210x295 | `w342` (342x513) | Smallest poster wider than what ships today. `w185` is narrower and lands visibly softer on the home-tab cards. |
| `image_medium` | still (episode) | 250x140 | `w300` (300x169) | Smallest still wider than today's. The next one down, `w185`, is 26% narrower. |
| `image_medium` | profile (person) | 210x295 | `w185` (185x278) | TMDB offers nothing between `w185` and `h632` (~421x632), so neither brackets 210x295. 12% narrower beats four times the pixels for an avatar in a credit list — and `PersonOut.image_original` is what the one large-headshot surface already prefers. |
| `image_original` | all | full size | `original` | The only faithful reading. |

Both halves are measured rather than taken from documentation. The sizes are a `GET /configuration` reading on 2026-08-12 — identical to NEU-1063's 2026-08-09 reading, plus `profile_sizes` (`w45 w185 h632 original`), which that one did not record. The TV Maze dimensions are pixels read off real `static.tvmaze.com` images, three per kind from the local mirror: 210x295, 250x140 and 210x295 exactly, in every sample.

## Backdrops stay unexposed

`backdrop_path` has no TV Maze ancestor, so no response field carries one and this ADR does not add one. Exposing it is an additive contract change with no consumer: the coverage audit (NEU-1031) files backdrops and the `images` namespace under Public Profiles & Sharing, which has not been built.

The mapping is recorded anyway — `BACKDROP`, `w780` — because `catalog.show.backdrop_path` and `catalog.image` are populated today, so the question is real the moment that project starts, and three lines now beats a re-measurement later.

## A null image is now normal, not exceptional

NEU-1042 deliberately did not copy TV Maze's image URLs into `poster_path`, because a full URL is not a path fragment. So between the copy and the ingest **every** show has no image, and a show TMDB never matches has none permanently — where under TV Maze a missing image meant an unusual show.

Composition maps an absent path to `None` rather than to a URL that would 404, and never to a half-present pair: a medium thumbnail whose `original` counterpart 404s is worse than two nulls, because the SPA's fallbacks key on the field being null. Those fallbacks already exist on every render path (`?? FALLBACK_POSTER`, `?? FALLBACK_HEADSHOT`, or a `? :` around the `<img>`) — verified across the SPA rather than assumed — which is why this ADR needs no frontend work to be safe.

## What this ADR does not do

Nothing calls `catalog/images.py` yet. Every read still goes to `tvmaze`, whose rows carry finished URLs already; **NEU-1047** is the pass that repoints browse, search, `/me` and credits to `catalog`, and it is the caller. This ticket owns the decision and the mechanism, that one owns the switch — separated because a decision and a repoint fail in different ways and deserve separate PRs.

One consequence lands on NEU-1047 rather than here: `CharacterRef.image_medium` has no TMDB source at all. TMDB models a character as free text, so `catalog.character` carries no image column and that field is permanently null after cutover — a fact about the source, not about composition.
