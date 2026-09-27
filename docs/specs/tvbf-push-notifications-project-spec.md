# tvbf: Push Notifications — project spec

**Linear project:** tvbf: Push Notifications (P-NEU-5) · initiative TV BingeFriend · team Neuroticsasquatch
**Repos:** `tvbf-backend` (this repo), `tvbf-frontend`
**Decisions recorded:** [ADR-0014](../adr/0014-episode-alerts-are-schedule-derived-and-events-are-tracked-only.md); vocabulary in `CONTEXT.md` § Notifications
**Written:** 2026-09-26, from the `/projectit` grilling session.
**Revised:** 2026-09-26, after checking the scaffolded tickets against the code — §4.3 FK rule,
§5.1 seam and run-kind read, §5.2 still-current date, `is_ended` naming. No decision changed.

This is the project-wide spec every
ticket in the project is implemented against unless a per-ticket spec (`docs/specs/NEU-xxxx-*.md`)
says otherwise. Frontend tickets cite it by this path from `tvbf-frontend`.

## 1. Purpose

Tell a user, on the device they carry, when something happens to a show they track: an episode
airs today, a season premiere date is announced or moves, or the show is ended or cancelled.
Delivery is Web Push, which on iOS requires the app to be installed to the Home Screen, so the
project also makes the SPA installable. Nothing else about the PWA (offline caching, update
prompts) is in scope.

## 2. Scope

**In:**

1. Change detection in the daily catalog delta, recorded as durable catalog events (tracked shows only).
2. A daily delivery job: the airs-today set plus undelivered catalog events → Web Push, with a
   freshness window, a per-user cap, an idempotent delivery log, subscription retirement and
   90-day purge.
3. VAPID keys in env, `pywebpush` as the sender, a public-key endpoint.
4. Push subscription storage and endpoints (many per user), a test-push endpoint, admin stats.
5. Per-user per-kind preferences on the existing preferences endpoint; a per-show mute on My Shows.
6. SPA: web manifest + icons, a hand-written service worker (`push`, `notificationclick` only),
   subscription lifecycle, a Notifications section in Settings (toggles, device list, test push,
   install button / iOS instructions), a one-time nudge card after the first My Shows add, a mute
   control on My Shows rows.

**Out (explicitly):** offline caching of any kind; friend-activity notifications; email delivery;
notification action buttons ("mark watched" from the notification); per-device preferences;
an in-app notification centre; a digest across shows.

## 3. Decisions (the grilling outcome)

| # | Question | Decision |
|---|---|---|
| Q1 | What is "a new episode"? | **Airs today** — a schedule fact read each morning, not a change event. No `episode_added` event exists (ADR-0014 §1). |
| Q2 | Which change events? | **premiere_set** (season premiere date null→date), **premiere_moved** (date→different date), **ended** (`show.status` enters `Ended`/`Canceled`), **revived** (`show.status` leaves `Ended`/`Canceled` — added mid-session; *not* tombstone resurrection). |
| Q3 | Detection mechanism | **Compare-on-upsert in Python**, in the per-show path the delta runs, before the show/season upsert. Not a trigger, not a snapshot. The full pass records nothing. |
| Q4 | Durability | Append-only **`catalog.show_event`** sidecar. The delta writes; the delivery job reads. |
| Q5 | Which shows are diffed | **Tracked shows only** — any row in `app.user_show_watch` (ADR-0014 §2). |
| Q6 | Burst guard | **Freshness window (48 h) + still-current check + per-user daily cap (5)**, overflow folded into one summary push. No run-level breaker. |
| Q7 | Delivery job | **Fifth Coolify scheduled task**, `python -m tvbf.jobs.push_deliver`, once daily after the catalog delta and airdate reconcile, own deadman `HEALTHCHECK_PUSH_URL`, `kind='push_deliver'` run row on `jobs/scheduled.py`. |
| Q8 | Grouping | **One push per show per kind.** Web Push `tag` = notification key so a re-send replaces. |
| Q9 | Idempotency | **`app.push_delivery`** row per (notification key, subscription), inserted before the send, unique. |
| Q10 | Sender + keys | **`pywebpush`**; `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_SUBJECT` env; `task vapid:generate`; `GET /push/vapid-public-key`. |
| Q11 | Subscriptions | **Many per user**, `app.push_subscription`, deleted on 404/410 or after 5 consecutive failures, never tied to a session. |
| Q12 | Preferences | **Per user, per kind** — five booleans on `app.user`, default on. Via `PATCH /me/preferences`. |
| Q13 | PWA shell | **Hand-written `public/sw.js` + `public/manifest.webmanifest`**; no `vite-plugin-pwa`, no precache. |
| Q14 | Permission | **Explicit button in Settings + one-time post-add nudge card.** Never on load; never from the Add click itself. iOS-not-installed shows install instructions. |
| Q15 | Payload | **Show name as title, one specific line as body, deep link** to the episode (`/episodes/{id}`) or show (`/shows/{id}`); poster as icon; `tag` = key. |
| Q16 | Airs-today rule | Corrected `air_date = today`, `season_number > 0`, show in My Shows, episode not watched, account not disabled, no email-verified gate. |
| Q17 | Retention | Delivery job's last step purges `show_event` + `push_delivery` older than 90 days. No separate task. |
| Q18 | Scope edges | In: test-push button, Android/desktop install prompt, admin stats endpoint, per-show mute. |

Assumption stated at the gate: a **mute silences every kind for that show**.

## 4. Data model

All `app.*` FKs cascade from `app.user`; catalog FKs follow the surrogate-id rule (ADR-0008).

### 4.1 `catalog.show_event` (sidecar, append-only)

| column | type | notes |
|---|---|---|
| `id` | bigserial PK | |
| `kind` | text, CHECK in (`premiere_set`, `premiere_moved`, `ended`, `revived`) | |
| `show_id` | int FK `catalog.show.id` ON DELETE CASCADE | |
| `season_id` | int FK `catalog.season.id` ON DELETE CASCADE, nullable | set for the two premiere kinds |
| `old_value` | text, nullable | ISO date or status string as it was |
| `new_value` | text, nullable | as it is now |
| `observed_at` | timestamptz not null default now() | |
| `run_id` | uuid FK `catalog.ingest_run.id`, nullable | the delta run that saw it |

Index `(observed_at)`; index `(show_id, kind)`. No uniqueness — a date that moves twice is two rows.

### 4.2 `app.push_subscription`

| column | type | notes |
|---|---|---|
| `id` | uuid PK default gen_random_uuid() | |
| `user_id` | uuid FK `app.user.id` CASCADE | |
| `endpoint` | text not null **unique** | the push service URL; an endpoint belongs to exactly one user |
| `p256dh` | text not null | client public key, base64url |
| `auth` | text not null | auth secret, base64url |
| `user_agent` | text, nullable | as sent at subscribe time; the Settings device list renders a label derived from it |
| `created_at` | timestamptz not null default now() | |
| `last_success_at` | timestamptz, nullable | |
| `failure_count` | int not null default 0 | consecutive; reset to 0 on any 2xx |

Index `(user_id)`. Re-subscribing with an endpoint that already exists (same or different user)
**upserts** the row onto the calling user — browsers re-issue the same endpoint after a
`pushsubscriptionchange`, and a device that changed hands should follow its current login.

### 4.3 `app.push_delivery`

| column | type | notes |
|---|---|---|
| `id` | bigserial PK | |
| `subscription_id` | uuid, nullable, FK `app.push_subscription.id` **SET NULL** | null once the subscription is retired; the row outlives it (see below) |
| `user_id` | uuid FK `app.user.id` CASCADE | denormalised for stats + the per-user cap |
| `notification_key` | text not null | see §5.3 |
| `kind` | text not null CHECK in (`airs_today`, `premiere_set`, `premiere_moved`, `ended`, `revived`, `summary`, `test`) | |
| `show_id` | int, nullable, FK `catalog.show.id` SET NULL | null for `summary`/`test` |
| `status` | text CHECK in (`pending`, `sent`, `failed`, `skipped`) | |
| `status_code` | int, nullable | push service HTTP status |
| `error` | text, nullable | |
| `created_at`, `sent_at` | timestamptz | |
| `run_id` | uuid FK `catalog.ingest_run.id`, nullable | |

**Unique `(notification_key, subscription_id)`** — the idempotency rule. A `pending` row is
inserted before the send inside the same transaction that then flips it; a crash leaves
`pending`, and the next run treats `pending` older than one hour as `failed` and re-sends.

**`subscription_id` is SET NULL, not CASCADE, on purpose.** Retirement (404/410 or the fifth
consecutive failure, §5.2 step 4) deletes the subscription, and the delivery row that recorded it
(`status='failed'`, `error='gone'` or `'failure_limit'`) must survive so `GET /admin/push/stats`
can count retirements per day (§5.4). A cascade would delete the evidence in the same statement.
Postgres treats NULLs as distinct in the unique index, so orphaned rows never collide with each
other; account deletion still cascades through `user_id`.

### 4.4 Columns added to existing tables

- `app.user`: `notify_airs_today`, `notify_premiere_set`, `notify_premiere_moved`,
  `notify_ended`, `notify_revived` — bool not null default true.
- `app.user_show_watch`: `muted` — bool not null default false. Rides beside
  `hide_from_activity` and is toggled the same way.

### 4.5 `catalog.ingest_run`

`ck_ingest_run_kind` gains `'push_deliver'` (NOT VALID, as the last three kinds were added).

## 5. Backend behaviour

### 5.1 Change detection (milestone 1)

In `tmdb/ingest.py:mirror_series`'s per-show transaction, **only when the run kind is
`catalog_update`** and the show is tracked (`EXISTS app.user_show_watch WHERE show_id`):

1. Before `upsert_series_payload` (`tmdb/upsert.py` — the one call the per-show path makes; it
   wraps `upsert_show` and `upsert_seasons`, which are not called from `ingest.py` directly),
   read the current `catalog.show.status` and, for every season row of the show,
   `(season_number, coalesce(tmdb_air_date, air_date))`.
2. After it returns (it yields only the show id; the season surrogate ids for the two premiere
   kinds are looked up by `(show_id, season_number)` in the same session), compute:
   - `ended`: old status not in (`Ended`, `Canceled`) and new status in it. Uses the same
     vocabulary as the generated `is_ended` column; the event records the raw status strings.
   - `revived`: old status in (`Ended`, `Canceled`) and new status not in it and not null. The
     mirror image of `ended`; a status going *null* is not an event. Unrelated to
     `deleted_upstream_at` — a tombstone resurrection records nothing here.
   - `premiere_set`: a season (number > 0) whose stored `air_date` was null (or whose row did not
     exist) and whose new `air_date` is non-null **and in the future** relative to today. A date
     that arrives already in the past is a backfill, not an announcement, and is not an event.
   - `premiere_moved`: a season (number > 0) whose stored `air_date` was non-null and whose new
     `air_date` is non-null and different, **and at least one of the two is in the future**.
     Historical corrections are not events.
3. Insert one `catalog.show_event` per transition, in the show's transaction, with the run id.

The comparison reads **TMDB's raw values** (`tmdb_air_date` on season, or `air_date` when no
offset applies) — the airdate offset projection is a later, separate pass and must not
manufacture a "moved" event by shifting a date one day. Concretely: compare
`coalesce(season.tmdb_air_date, season.air_date)` old vs the incoming payload's `air_date`.

Tracked-ness is evaluated once per show at detection time; a show tracked by nobody records
nothing (ADR-0014 §2). The full pass (`catalog_initial`) and every backfill job skip detection
entirely — the hook is keyed on the run kind, not on the caller remembering to pass a flag.
`mirror_series` receives only `run_id` today and `catalog/runs.py` has no kind reader, so the
hook reads `catalog.ingest_run.kind` once per run through a new `runs.py:get_run_kind(session,
run_id)` and caches it for the loop; `upsert.py` itself is not touched.

### 5.2 The delivery job (milestone 3)

`python -m tvbf.jobs.push_deliver`, `jobs/scheduled.py` shape (run row `kind='push_deliver'`,
`HEALTHCHECK_PUSH_URL` deadman, exit code is the result, work awaited never spawned). Schedule in
Coolify **daily at 13:00 UTC** (09:00 US Eastern), after the catalog delta and the airdate
reconcile; nothing in the repo can enforce the order, and the cost of getting it wrong is an
alert that fires a day late. `today` is `date.today()` in the container (UTC), which at 13:00 UTC
is the US-Eastern date — the same clock `/me/upcoming` reads.

Refuses to start (`exit 1`, logged) when `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY` or
`VAPID_SUBJECT` is unset. An in-flight guard per kind, as the other run-row jobs have.

Steps, in order:

1. **Airs-today candidates.** For every user with ≥1 subscription and `notify_airs_today`, and
   `disabled_at IS NULL`: episodes where `episode.air_date = today`, `season_number > 0`,
   the show is in the user's My Shows with `muted = false`, and no `user_episode_watch` row.
   Notification key `airs_today:{episode_id}:{air_date}`.
2. **Event candidates.** `catalog.show_event` rows with `observed_at >= now() - 48h` **and still
   current** — `premiere_set`/`premiere_moved`: the season's **raw** date
   (`coalesce(tmdb_air_date, air_date)`, the same value §5.1 compared and stored in `new_value`;
   the corrected `air_date` differs by the offset for every offset-corrected show and would never
   match) still equals `new_value`, and the season's corrected `air_date` is today or later;
   `ended`: the show's `is_ended` is still true; `revived`: `is_ended` is still false — joined to users who
   track the show, are not muted on it, have the matching `notify_*` flag, and are not disabled.
   Key `{kind}:{event_id}`. Events older than 48 h are never delivered; nothing marks them,
   the window is the rule.
3. **Per-user cap.** Order a user's candidates airs-today first (by show name), then events by
   `observed_at`. Take the first 5; if more remain, replace the remainder with one `summary`
   notification ("and N more updates today" → `/upcoming`), key `summary:{user_id}:{today}`.
4. **Deliver.** For each (candidate, subscription of that user): insert `push_delivery`
   `pending` (skip the candidate if the unique constraint says it was already `sent`), build
   the payload (§5.3), `webpush()` with `TTL=86400` and `urgency=normal`. On 2xx → `sent`,
   `last_success_at = now()`, `failure_count = 0`. On 404/410 → `failed`, `error='gone'`, and
   **delete the subscription**. On any other non-2xx or transport error → `failed`,
   `failure_count += 1`, and delete the subscription when it reaches 5 (that row's `error` is
   `'failure_limit'`). The delivery rows survive the delete (§4.3). Sequential per subscription; a bounded semaphore is
   the fix at scale, not a rewrite.
5. **Purge.** Delete `show_event` and `push_delivery` rows with `created_at < now() - 90 days`.
6. Finalize the run row `succeeded` with counts logged (candidates, sent, failed, retired,
   purged). **Exit 1 only if every send failed** — a push service outage — never for individual
   failures, which are the log's business.

### 5.3 Payload contract (shared with the service worker)

JSON, encrypted by `pywebpush`, decoded in `sw.js`:

```json
{
  "key": "airs_today:12345:2026-09-26",
  "kind": "airs_today",
  "title": "Severance",
  "body": "S2E4 “Woe’s Hollow” airs today",
  "url": "/episodes/12345",
  "icon": "https://image.tmdb.org/t/p/w185/abc.jpg"
}
```

- `title` is always the show name; `body` per kind: airs-today `S{s}E{e} “{title}” airs today`
  (`S{s}E{e} airs today` when the episode has no title); `premiere_set` `Season {n} premieres
  {Mon D}`; `premiere_moved` `Season {n} moved to {Mon D}` (`…date removed` when `new_value` is
  null is **not** a kind — a date going null is not an event); `ended` `Marked as ended` or
  `Marked as cancelled` from the raw status; `revived` `Renewed — more episodes are coming`; `summary` title `TV BingeFriend`, body
  `{N} more updates today`; `test` title `TV BingeFriend`, body `Notifications are working`.
- `{Mon D}` in the two premiere bodies is the season's **corrected** `air_date` as of delivery —
  what the app shows (NEU-1145) — not `new_value`, which is the raw TMDB date §5.1 stores for
  comparison.
- `url` is SPA-relative; the worker prefixes `self.location.origin`. `icon` is the poster via
  `catalog/images.py` at `w185`, or omitted when null. `tag` on the notification = `key`.
- The worker never fetches; everything it shows is in the payload. Payload stays under 4 KB.

### 5.4 Endpoints (milestone 3 + 4)

All under the cookie session; mutating ones require CSRF. Documented in
`.claude/docs/architecture-endpoints.md` when built.

| method + path | auth | body / response | notes |
|---|---|---|---|
| `GET /push/vapid-public-key` | none | `{ "public_key": "<base64url>" }` | 503 when unset. Cache-Control public, 1 day. |
| `GET /me/push/subscriptions` | user | `[{ id, user_agent, created_at, last_success_at }]` | never returns keys or the endpoint |
| `POST /me/push/subscriptions` | user + CSRF | `{ endpoint, keys: { p256dh, auth } }` (the `PushSubscription.toJSON()` shape) → 201 `{ id }` | upsert on endpoint (§4.2); reads `User-Agent` |
| `DELETE /me/push/subscriptions/{id}` | user + CSRF | 204 | 404 if not the caller's |
| `DELETE /me/push/subscriptions` | user + CSRF | 204 | "turn everything off": deletes all of the caller's |
| `POST /me/push/test` | user + CSRF | `{ subscription_id }` → 202 | sends a `test` payload now, logged as `kind='test'`; 404 if not the caller's; throttled 5/hour per user (`app.auth_attempt` pattern) |
| `PATCH /me/preferences` | user + CSRF | adds optional `notify_airs_today`, `notify_premiere_set`, `notify_premiere_moved`, `notify_ended`, `notify_revived` | `AuthedUserOut` grows the same five fields |
| `PATCH /me/shows/{show_id}/mute` | user + CSRF | `{ muted: bool }` → 204 | mirrors `hide-from-activity`; `GET /me/shows` entries gain `muted` |
| `GET /admin/push/stats` | admin (cookie) | `{ subscriptions, users_subscribed, by_day: [{ day, sent, failed, retired }] }` (last 30 days) | read-only over the two tables |

### 5.5 Settings and ops

- `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY` (base64url raw keys as `py-vapid` emits), `VAPID_SUBJECT`
  (`mailto:` or the app URL), `HEALTHCHECK_PUSH_URL`, `PUSH_DAILY_CAP` (default 5),
  `PUSH_EVENT_WINDOW_HOURS` (default 48). All optional in `Settings`; the job and the endpoints
  refuse rather than default.
- `task vapid:generate` runs `vapid --gen` equivalent via `py_vapid` and prints the three values.
- `task push:deliver` is the manual trigger, mirroring `task update:catalog`.
- `docs/migration/README.md` is **not** touched — this is not a migration pass. The runbook for
  first deploy (generate keys, set env, add the Coolify task, add the healthcheck) goes in the
  delivery ticket's PR description and in `README.md`'s env table.

## 6. Frontend behaviour

### 6.1 Manifest and worker (milestone 2)

- `public/manifest.webmanifest`: `name` TV BingeFriend, `short_name` BingeFriend,
  `start_url` `/`, `display` `standalone`, `background_color`/`theme_color` `#0f1729`, icons
  192 and 512 (`any` and `maskable` variants) generated from `favicon.svg`. Linked from
  `index.html` with `<link rel="manifest">` and `apple-touch-icon` (180 px PNG).
- `public/sw.js`: registers no caches. Handles `push` (parse JSON, `showNotification(title,
  { body, icon, tag: key, data: { url } })`), `notificationclick` (close; focus an existing client
  matching the origin and navigate it, else `openWindow`), and `pushsubscriptionchange`
  (re-subscribe with the stored applicationServerKey and `POST` the new subscription — the one
  fetch the worker makes, credentials `include`, and it is best-effort). `install` calls
  `skipWaiting`, `activate` calls `clients.claim`.
- Registered from `src/main.tsx` **after** first render, only when `"serviceWorker" in navigator`.
  Registration is unconditional (it is what installability needs), permission is not.
- Cloudflare Pages serves `public/` as-is; `sw.js` at the root gets root scope. Verify the
  `Service-Worker-Allowed` header is not needed (root scope is the default for a root file).

### 6.2 Subscription lifecycle (milestone 3)

`src/lib/push.ts` owns: `supportState()` → `unsupported | ios_needs_install | denied | prompt |
granted`; `subscribe()` → `Notification.requestPermission()` **only from a click handler**, then
`pushManager.subscribe({ userVisibleOnly: true, applicationServerKey })` with the key from
`GET /push/vapid-public-key`, then `POST /me/push/subscriptions`; `unsubscribe()` → local
`subscription.unsubscribe()` then `DELETE` by id. iOS detection: `navigator.standalone !== true`
on an iOS user agent means "installed first". `src/api/push.ts` holds the hooks, with
`["me-push-subscriptions"]` as the query key.

### 6.3 Settings → Notifications section (milestone 4)

Beneath Privacy in `SettingsPage`, `aria-labelledby="notifications-heading"`:

- State line per `supportState()`: unsupported → explanatory text; `ios_needs_install` →
  "Add to Home Screen" instructions (share sheet → Add to Home Screen); `denied` → how to
  re-enable in browser settings; `prompt` → **Turn on notifications** button; `granted` and
  subscribed → "On for this device".
- **Install app** button when a captured `beforeinstallprompt` exists (Chromium); hidden otherwise.
- Five toggles (one per kind), live-saved through `PATCH /me/preferences`, disabled with a hint
  when no device is subscribed.
- Device list from `GET /me/push/subscriptions`: label from `user_agent`, added date, last
  delivered, a Remove button per row, and "Turn off everywhere".
- **Send test notification** button, enabled when this device is subscribed.

### 6.4 Nudge card (milestone 4)

After a successful `useAddShow` mutation, if `supportState()` is `prompt` or `ios_needs_install`
and `localStorage["push-nudge-dismissed"]` is unset, render a dismissible card on the page the
add happened from ("Get told when {show} airs" → the same button / instructions as Settings).
Shown at most once per browser: dismissing **or** acting sets the key. `usePersistedString` is
not used here (it is unvalidated by design); a two-line `try/catch` around `localStorage` is.

### 6.5 Mute (milestone 4)

`MyShowCard` / `LibraryActiveList` rows gain a mute toggle (lucide `BellOff` / `Bell`) in the
action row, honoured only when `ratingOwner.kind === "own"` like `removable`. `GET /me/shows`'s
`muted` drives it; `PATCH /me/shows/{id}/mute` flips it, optimistic, invalidating `["me-shows"]`.
Muting does not invalidate `["me-recommendations"]` — it is not a never-recommend source.

## 7. Cross-cutting rules

- **The SPA never hard-codes the VAPID public key**; it is fetched. A key rotation is an env
  change and every existing subscription becomes invalid (the push service rejects the JWT),
  which the 404/410 rule retires over the following days.
- **No secrets reach the browser** (frontend CLAUDE.md rule). The private key is server-only.
- **ADR-0002 holds**: no request-path call leaves Postgres; the push service is called only
  from the job and from `POST /me/push/test`, which is a user-initiated background send.
- **`app.push_delivery` is append-only in spirit**: rows are updated only from `pending` to a
  terminal status; nothing rewrites history. The purge is the only delete.
- **Disabled accounts receive nothing** and keep their subscriptions (re-enable restores them).
  Account deletion cascades everything.
- **Export (`GET /me/export`)** gains the user's subscriptions (id, user_agent, created_at) and
  preferences — it exists to be complete.

## 8. Testing

- Backend unit: detection over a fake old-row/new-payload pair for each of the four kinds and
  each non-event (backfilled past date, one-day offset shift, untracked show); the candidate
  query against seeded rows for each Q16 clause; the cap and summary; idempotency on a second
  run; 404/410 retirement; `pywebpush` mocked at the module boundary (`respx` is not the right
  seam — the library builds the request).
- Backend integration: the job end-to-end against the test database with a stubbed `webpush`.
- Frontend: `sw.js` is tested by a small vitest suite that imports it in a fake `self` (the
  handlers are pure over `event.data.json()`); `push.ts` state machine under MSW; Settings
  section renders each `supportState`; nudge shows once.

## 9. Milestone map

Refined in Linear; the intended split:

1. **Catalog change events** — §4.1, §4.5, §5.1. Backend only.
2. **PWA shell** — §6.1. Frontend only; parallel with 1.
3. **Push subscriptions and delivery** — §4.2–4.4 (subscription, delivery, VAPID), §5.2–5.5,
   §6.2, plus the Settings state line and Turn-on button (the minimum to subscribe a device).
4. **Preferences, mute, nudge, install, admin** — remaining §5.4 rows, §6.3–6.5, export.
