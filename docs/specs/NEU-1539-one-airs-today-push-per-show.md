# NEU-1539 — One airs-today push per show per day

**Ticket:** [NEU-1539](https://linear.app/neuroticsasquatch/issue/NEU-1539/limit-new-episode-notifications-to-one-per-show)
**Project:** tvbf: Maintenance · no parent, no milestone, no relations
**Repo:** `tvbf-backend` only. No frontend change: `sw.js` reads `key`, `title`,
`body`, `url` and `icon`, all of which stay strings. No API change, no migration.
**Written:** 2026-10-07

A streaming drop puts a whole season on one air date. `airs_today_candidates`
yielded one candidate per **episode**, keyed `airs_today:{episode_id}:{date}`,
so an eight-episode drop was eight pushes — and because airs-today candidates
sort first in `apply_cap`, the drop filled the five-slot daily cap and every
other show and every catalog event that day collapsed into the summary push.

This is the code falling short of the design, not a new decision. The push
project spec's Q8 says *one push per show per kind*, and `CONTEXT.md` defines a
**Notification** as *one push about one show for one user, of exactly one
kind*. Only §5.2 step 1, §5.3 and ADR-0014's consequences line still spelt the
key per episode, and the code followed those three sentences. This spec fixes
the code and the three sentences.

---

## 1. One candidate per show (`push/candidates.py`)

The airs-today query is unchanged — every Q16 clause stands — but its rows are
ordered `(user, show, season_number, episode_number, id)` and folded: every
episode of one show airing today for one user becomes **one** `Candidate`.

- `key = airs_today:{show_id}:{air_date}` — **always** show-based, including
  for a show with a single episode today. One key shape, not two, so a show
  whose episode set changes between runs still maps to one delivery-log row per
  day, and the glossary's definition of the key ("its kind plus the show and
  the … it is about") stays true.
- A new frozen `AiredEpisode(id, season_number, episode_number, name)` and a
  new `Candidate.episodes: tuple[AiredEpisode, ...]` carry the whole group, in
  (season, episode) order.
- The existing single fields — `episode_id`, `season_number`,
  `episode_number`, `episode_name` — are the **first** episode's. That is what
  the one-episode body, the `/episodes/{id}` link and `apply_cap`'s existing
  sort key read, so a single episode renders identically whether or not it
  came through a group.
- `apply_cap` is unchanged in logic. A drop now costs one cap slot, and the
  summary's `count` and `show_names` count notifications (shows), as the body
  "N more updates today" always claimed.

**Stated trade-off.** A same-day re-run after an episode is watched, or after
a date moves onto today, is a *skip*: the key is already `sent`. The job runs
once a day, and this is the spec's existing idempotency stance (§4.3); nothing
here makes a second run on one day re-notify.

## 2. Payload (`push/payloads.py`)

`_body` and `_url` for `airs_today` branch on how many episodes the candidate
carries:

| episodes | body | url |
|---|---|---|
| 0 or 1 | unchanged: `S2E4 “Woe’s Hollow” airs today`, or `S2E4 airs today` without a title | `/episodes/{episode_id}` (unchanged) |
| ≥ 2, one season, unbroken run | `8 episodes air today (S2E1–E8)` | `/shows/{show_id}/episodes?season={first season}` |
| ≥ 2, otherwise | `3 episodes air today (S2E1, S2E3, S3E1)` — as many whole codes as fit, then `and {k} more` | same |

- The count is the news and the codes are the detail; episode titles are
  dropped from a grouped body — they do not fit, and the single-episode body
  still carries one.
- Title stays the show name, `icon` the poster. The grouped body stays under
  `MAX_TEXT_CHARS` by construction (the codes get whatever room the fixed
  words leave) and so under the 4 KB envelope; the whole-item fitting is the
  summary's `_show_list`, generalised into `_list_that_fits`.
- `?season=` is a real SPA route (`router.tsx` `shows/:id/episodes`,
  `EpisodesPage` reads the param), and the worker's `appUrl` resolves the path
  with `new URL(path, origin)`, so a query string survives the click.

## 3. Delivery (`push/delivery.py`)

No change. `claim_for_run` already takes `candidate.key` and
`candidate.show_id`; `push_delivery.show_id` was already per show and
`kind='airs_today'` already passes the CHECK, so there is no migration.

**Deploy-day note.** Rows logged earlier that day under the old per-episode
keys do not match the new key, so a *manual* `task push:deliver` re-run after
the 13:00 UTC run on deploy day would send that day's airs-today pushes once
more. Don't re-run by hand that day.

## 4. Documentation

- `docs/specs/tvbf-push-notifications-project-spec.md`: §5.2 step 1 key and
  "one candidate per show"; §5.3 example key and the grouped body/url rule;
  Q15's deep link gains the episodes-page form. Each edit marked `(NEU-1539)`.
- `docs/adr/0014-…`: consequences line — the key is the **show** and the air
  date.
- `CONTEXT.md` **Airs-today set** and **Notification key**: one push per show,
  keyed on show + air date.
- `.claude/CLAUDE.md` `task push:deliver` paragraph: one sentence.

## 5. Tests

- `tests/integration/push/test_candidates.py`: the key assertions; three
  episodes of one show fold into one candidate in (season, episode) order with
  the first's single fields; a watched episode leaves the rest of the drop; two
  shows are two candidates.
- `tests/unit/push/test_candidates.py`: the `_airs` helper keys on a show; a
  season dump is one candidate against the cap; the summary still names a show
  once when an event and an airs-today push share it.
- `tests/unit/push/test_payloads.py`: a group of one renders as the single
  episode; the range, the two-episode range, a broken run, a cross-season group
  and its link, a long broken list keeping whole codes, titles dropped, the
  4 KB worst case for a 300-episode group.
- `tests/integration/jobs/test_push_deliver_cli.py`: the three tests that need
  two candidates use a second *show* (a second episode of the first show now
  folds); one end-to-end — eight episodes of one show plus an `ended` event on
  another is three sends, three log rows, no summary, and the drop's body names
  the range.

## 6. What this does not do

- Does not group **across** shows (the spec's "Out" list), and does not merge
  kinds: a premiere and an airs-today for one show are still two pushes.
- Does not change the cap, the summary, the freshness window, the still-current
  check, or any event kind.
- Does not change the key of any event kind (`{kind}:{event_id}`) or the
  summary (`summary:{user_id}:{today}`).
- Does not re-notify on a same-day re-run when the episode set changes (§1).
- Does not touch the frontend; `src/sw.test.ts`'s fixture carries the old key
  string as opaque data and stays as it is.

## 7. Acceptance criteria

1. A user tracking a show with N ≥ 2 unwatched regular episodes dated today
   receives **one** push for it, body `N episodes air today (S{s}E{a}–E{b})`
   for an unbroken run of one season, clicking through to
   `/shows/{id}/episodes?season={s}`; `app.push_delivery` holds one
   `airs_today:{show_id}:{date}` row per device.
2. A single episode today renders exactly as before, apart from the key.
3. That drop spends one of the user's five daily slots: four other shows or
   events the same day are all delivered and no summary is sent.
4. `task test`, `task lint` and `task typecheck` pass.
