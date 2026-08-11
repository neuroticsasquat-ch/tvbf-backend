-- Move the two user-touched unmatched shows onto their TMDB rows (NEU-1066).
--
-- NOT YET EXECUTED, and it CANNOT RUN YET. `app.user_show_watch.show_id` still
-- carries a foreign key to `tvmaze.show.id`, and both destinations are catalog
-- surrogates the TMDB ingest minted — 1202502 and 1067768, well above TV Maze's
-- highest id of 93,499 — so the INSERT below raises a foreign key violation
-- today. **Run this after NEU-1046 repoints `app.*` at `catalog.*`**, then update
-- the run log in README.md and re-run `task prune:shows` to take the freed copies.
--
-- Until then the two rows are NEU-1066's enumerated, accepted exceptions: they
-- stay reachable and fully functional, and `still_doubled` names "Discretion" as
-- a show that appears twice.
--
-- Why this is SQL run by hand rather than part of the pass: it moves *user data*,
-- and `show_prune` deliberately does not. Every other decision in that pass is
-- reversible by re-copying from `tvmaze`; this one changes what a person sees on
-- their own list, so it is a decision to make once, in the open, with the two
-- rows named.
--
-- Why the pass cannot map these instead: `queue:confirm` writes a `tmdb_id` onto
-- the copied row, and the full ingest has already inserted a row holding that id.
-- `uq_show_tmdb_id` refuses. NEU-1044's window closed when the ingest ran, which
-- is the same wall NEU-1119 hit at season grain.
--
-- Both rows were checked against the live TMDB API on 2026-08-11 and neither show
-- is absent from TMDB:
--
--   87519 "Discretion"    -> TMDB 300966, ingested as catalog.show 1202502.
--                           A plain duplicate.
--   63900 "Cunk on Earth" -> TMDB models this as SEASON 2 of "Cunk on..."
--                           (TMDB 79063, ingested as catalog.show 1067768,
--                           seasons 1 "Britain" and 2 "Earth"). TV Maze models it
--                           as its own show. A grain mismatch, not an absence.
--
-- **The Cunk row is a visible change for that user**: their My Shows entry stops
-- reading "Cunk on Earth" and starts reading "Cunk on...". That is what TMDB
-- actually carries, and the alternative is holding a locally-authored duplicate
-- of a series TMDB has. Decided knowingly.
--
-- Each show is exactly one `app.user_show_watch` row and nothing else — no
-- episode watches, no ratings, no activity events — verified below in step 1
-- rather than trusted from this comment, because that is what makes a bare
-- UPDATE the whole job.

-- ---------------------------------------------------------------------------
-- Step 1 — confirm the shape has not changed since 2026-08-11.
-- Every count must be 0 except `tracked`, which must be 1 for each row. If an
-- episode watch or rating has appeared since, STOP: the move then needs the
-- episode grain handled too, which this file does not do.
-- ---------------------------------------------------------------------------

SELECT c.id,
       c.name,
       (SELECT count(*) FROM app.user_show_watch w WHERE w.show_id = c.id) AS tracked,
       (SELECT count(*) FROM app.user_show_rating r WHERE r.show_id = c.id) AS show_ratings,
       (SELECT count(*) FROM app.user_episode_watch w
          JOIN tvmaze.episode e ON e.id = w.episode_id
         WHERE e.show_id = c.id) AS episode_watches,
       (SELECT count(*) FROM app.user_episode_rating r
          JOIN tvmaze.episode e ON e.id = r.episode_id
         WHERE e.show_id = c.id) AS episode_ratings,
       (SELECT count(*) FROM app.activity_event a
         WHERE a.target_type = 'show' AND a.target_id = c.id) AS show_events,
       (SELECT count(*) FROM app.activity_event a
          JOIN tvmaze.episode e ON e.id = a.target_id
         WHERE a.target_type = 'episode' AND e.show_id = c.id) AS episode_events
  FROM catalog.show c
 WHERE c.id IN (87519, 63900);

-- And confirm the destinations are the rows this file thinks they are.
SELECT id, tmdb_id, name FROM catalog.show WHERE id IN (1202502, 1067768);

-- ---------------------------------------------------------------------------
-- Step 2 — move the My Shows rows.
--
-- `ON CONFLICT DO NOTHING` then `DELETE` rather than a bare UPDATE: if that user
-- already tracks the destination show, the update would violate
-- `user_show_watch`'s (user_id, show_id) key and take the transaction with it.
-- Insert-then-delete collapses to the same result either way, and keeps the
-- earlier `created_at` when both exist — real events outrank a migration's.
-- ---------------------------------------------------------------------------

BEGIN;

INSERT INTO app.user_show_watch (user_id, show_id, created_at)
SELECT w.user_id,
       CASE w.show_id WHEN 87519 THEN 1202502 WHEN 63900 THEN 1067768 END,
       w.created_at
  FROM app.user_show_watch w
 WHERE w.show_id IN (87519, 63900)
    ON CONFLICT (user_id, show_id) DO NOTHING;

DELETE FROM app.user_show_watch WHERE show_id IN (87519, 63900);

-- Expect 2 rows: the user now tracks 1202502 and 1067768.
SELECT user_id, show_id FROM app.user_show_watch WHERE show_id IN (1202502, 1067768);

COMMIT;

-- ---------------------------------------------------------------------------
-- Step 3 — the copies are now untouched, so the pass will take them.
--
--   task prune:shows:report   -- `kept_user_touched` should be 0, `still_doubled` empty
--   task prune:shows
--
-- Then re-run the reconciliation harness. It counts per (user, show), so this
-- move IS a difference and `verify` will report it — that is the harness working,
-- not a failure. Re-capture the baseline afterwards so the cutover gate compares
-- against the post-remediation truth.
-- ---------------------------------------------------------------------------
