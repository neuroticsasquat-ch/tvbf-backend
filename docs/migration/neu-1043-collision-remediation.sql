-- Give back the tmdb_ids a tier-3 guess took from an exact match (NEU-1065).
--
-- ## STATUS: executed against production 2026-08-10
--
-- 107 collisions, of which **18 were repairable** — 18 guesses retracted, 18
-- exact matches stamped (16 `tvdb_id`, 2 `imdb_id`), unmatched count unchanged
-- because 18 rows left it and 18 entered. Of the remainder, 49 were "two exact
-- claims" (both rows resolve to one TMDB series by exact external id — genuine
-- source duplicates, NEU-1066's first tranche) and 42 were correct refusals of a
-- guess. **No collision row on either side carried user data.**
--
-- Kept runnable rather than reduced to a record: another enrichment pass before
-- NEU-1065 lands would produce the same class of collision, and the inspection
-- queries in step 3 are reusable on any run's log.
--
-- ## What it repairs
--
-- NEU-1043 orders the mapping tiers correctly *within* a show but not *between*
-- shows: candidates stream in `ORDER BY id`, and the first row to claim a
-- `tmdb_id` keeps it. So a tier-3 title guess on a low-id row can beat a tier-1
-- exact `/find` hit on a higher-id row. The worked example:
--
--   catalog show 4822 matched TMDB 747 via tvdb_id, but another catalog row
--   already holds it — left unmatched for the human queue
--
--   4821  Lads Army      tvdb 141551            2002   won 747 via title_year
--   4822  Bad Lads Army   tvdb 85570  tt0417301  2004   lost, had an exact hit
--
-- TMDB's own external-id link says 747 <-> tvdb 85570 <-> Bad Lads Army, so the
-- surviving guess was a false positive. It won only because 4821 < 4822. Four
-- more of the 18 were the same shape with titles that are not even similar —
-- e.g. TMDB 29492 guessed by *Great Performances at the Met* while
-- *Metropolitan Opera: Live in HD* held the exact tvdb link.
--
-- ## What it does NOT repair
--
-- A tier-3 false positive that contested nothing — a guess onto a TMDB id no
-- other row claims — is invisible to both the log and the database. Those
-- surface only by reading `match_method = 'title_year'` rows by hand. This
-- script is not a substitute for that check.


-- =====================================================================
-- STEP 1 — build the input. This is not SQL, and it is why the file
-- cannot simply be run.
--
-- The collisions exist ONLY in the run's log. The database records that a row
-- is unmatched, never that it lost a contest or to whom. If the log is gone, so
-- is the input, and the only route back is re-running enrichment and capturing
-- the warnings afresh.
--
--   docker exec <tvbf-backend-container> \
--     grep 'already holds it' /tmp/enrich.log \
--     | sed -E "s/.*catalog show ([0-9]+) matched TMDB ([0-9]+) via ([a-z_]+),.*/        (\1, \2, '\3'),/" \
--     | sed '$ s/,$//' | tee ~/neu-1043-collisions.txt
--
-- Save it on the **host**, not in the container: a redeploy replaces the
-- container and takes /tmp with it.
-- =====================================================================


-- =====================================================================
-- STEP 2 — load it, then prove the load is faithful.
--
-- A temp table rather than a CTE repeated at every use site: at ~100 rows the
-- CTE form means pasting the whole list three times, and the 2026-08-10 run
-- proved why that matters — a paste duplicated two rows and the verdict tally
-- silently summed to 109 against a known 107. Load once, verify, reuse.
--
-- Note psql running under `docker exec` cannot see host files, so `\i` and
-- `\copy` of ~/neu-1043-collisions.txt do not work from an interactive session.
-- Either paste the INSERT, or pipe the whole script in with
-- `docker exec -i <pg> psql ... < file`.
-- =====================================================================
CREATE TEMP TABLE collision(loser_id bigint, tmdb_id int, loser_method text);

INSERT INTO collision VALUES
    (4822, 747, 'tvdb_id')                             -- <<< COLLISIONS >>>
;

-- Must equal `wc -l ~/neu-1043-collisions.txt`.
SELECT count(*) AS loaded FROM collision;

-- Must be empty. A show is considered once and matches at most once, so two
-- rows for one loser can only be a paste artifact, never a second collision.
SELECT loser_id, count(*) FROM collision GROUP BY 1 HAVING count(*) > 1;

-- If it is not empty, this removes only rows identical on all three columns —
-- so a genuine discrepancy survives and the count stays wrong, which is the
-- signal to reload from the file rather than to reason about it.
--
-- DELETE FROM collision a USING collision b
-- WHERE a.ctid < b.ctid
--   AND a.loser_id = b.loser_id AND a.tmdb_id = b.tmdb_id
--   AND a.loser_method = b.loser_method;


-- =====================================================================
-- STEP 3 — INSPECT. Read all three before changing anything.
-- =====================================================================

-- 3a. How many are actually repairable?
SELECT
    CASE
        WHEN w.id IS NULL THEN 'leave - nothing holds this id'
        WHEN w.match_method = 'title_year' AND c.loser_method IN ('tvdb_id','imdb_id')
            THEN 'RETRACT - a guess is sitting on an exact match''s id'
        WHEN w.match_method IN ('tvdb_id','imdb_id') AND c.loser_method IN ('tvdb_id','imdb_id')
            THEN 'leave - two exact claims, a real upstream conflict (NEU-1066)'
        ELSE 'leave - the exact match already won'
    END AS verdict,
    count(*)
FROM collision c
LEFT JOIN catalog.show w ON w.tmdb_id = c.tmdb_id
GROUP BY 1 ORDER BY 2 DESC;

-- Only RETRACT rows are touched by step 5. Two *exact* claims on one TMDB id is
-- a genuine upstream conflict: retracting there would be picking a winner by
-- hand on no better evidence than the job had.

-- 3b. The repairable rows in full, for eyeballing before committing.
SELECT c.tmdb_id,
       c.loser_id, l.name AS loser_name, c.loser_method AS loser_tier,
       w.id AS winner_id, w.name AS winner_name
FROM collision c
JOIN catalog.show l ON l.id = c.loser_id
JOIN catalog.show w ON w.tmdb_id = c.tmdb_id
WHERE w.match_method = 'title_year'
  AND c.loser_method IN ('tvdb_id','imdb_id')
ORDER BY c.tmdb_id;

-- 3c. Does any of this touch user data?
--
-- **Both sides**, and the winner side is the one that matters more: step 5
-- strips a winner's `tmdb_id`, so a user-touched winner would be silently
-- unmapped. The 2026-08-10 run checked only losers at first, which was the
-- weaker half of the question.
--
-- `activity_event` is polymorphic with **no foreign key** — it neither blocks a
-- delete nor cascades, it just orphans, which is exactly the hazard ADR-0005
-- cites. Confirm the vocabulary with `SELECT DISTINCT target_type FROM
-- app.activity_event` before trusting the literal below.
WITH involved AS (
    SELECT loser_id AS show_id FROM collision
    UNION
    SELECT w.id FROM collision c JOIN catalog.show w ON w.tmdb_id = c.tmdb_id
)
SELECT i.show_id, s.name, s.match_method,
       (SELECT count(*) FROM app.user_show_watch  x WHERE x.show_id = i.show_id) AS tracked,
       (SELECT count(*) FROM app.user_show_rating x WHERE x.show_id = i.show_id) AS rated,
       (SELECT count(*) FROM app.user_episode_watch uew
          JOIN tvmaze.episode e ON e.id = uew.episode_id WHERE e.show_id = i.show_id) AS ep_watches,
       (SELECT count(*) FROM app.user_episode_rating uer
          JOIN tvmaze.episode e ON e.id = uer.episode_id WHERE e.show_id = i.show_id) AS ep_rated,
       (SELECT count(*) FROM app.activity_event a
        WHERE a.target_id = i.show_id AND a.target_type = 'show') AS events
FROM involved i
JOIN catalog.show s ON s.id = i.show_id
WHERE EXISTS (SELECT 1 FROM app.user_show_watch  x WHERE x.show_id = i.show_id)
   OR EXISTS (SELECT 1 FROM app.user_show_rating x WHERE x.show_id = i.show_id)
   OR EXISTS (SELECT 1 FROM app.user_episode_watch uew
                JOIN tvmaze.episode e ON e.id = uew.episode_id WHERE e.show_id = i.show_id)
   OR EXISTS (SELECT 1 FROM app.user_episode_rating uer
                JOIN tvmaze.episode e ON e.id = uer.episode_id WHERE e.show_id = i.show_id)
   OR EXISTS (SELECT 1 FROM app.activity_event a
              WHERE a.target_id = i.show_id AND a.target_type = 'show');

-- Empty on 2026-08-10. Any row here means a human decides that one, not this
-- script.


-- =====================================================================
-- STEP 4 — GUARD. Must return zero rows before step 5.
--
-- If two *exact* losers ever contested the same freed `tmdb_id`, step 5b would
-- try to stamp both and abort the transaction on `uq_show_tmdb_id`. Multi-loser
-- contests are real — TMDB 447 had two — they simply happened to be `title_year`
-- losers on 2026-08-10 and so were excluded. Do not assume that holds.
-- =====================================================================
SELECT tmdb_id, count(*) FROM collision
WHERE loser_method IN ('tvdb_id','imdb_id')
GROUP BY 1 HAVING count(*) > 1;


-- =====================================================================
-- STEP 5 — FIX. Two statements, one transaction, in this order.
--
-- Deliberately not one statement: `uq_show_tmdb_id` is not deferrable, and a
-- single command that frees an id and re-takes it can trip the unique index
-- part-way through.
--
-- **The two rowcounts must match each other and the RETRACT tally**, otherwise
-- an id was freed and never re-taken. ROLLBACK is free until COMMIT.
-- =====================================================================
BEGIN;

-- 5a. Retract the suspect guess. It reverts to `tmdb_id IS NULL`, a legitimate
--     terminal state under ADR-0008, not a broken row.
UPDATE catalog.show w
SET tmdb_id = NULL, match_method = NULL
FROM collision c
WHERE w.tmdb_id = c.tmdb_id
  AND w.match_method = 'title_year'
  AND c.loser_method IN ('tvdb_id','imdb_id');

-- 5b. Hand the id to the row that matched it exactly. The NOT EXISTS mirrors
--     the job's own guard, so a loser whose winner was itself an exact match is
--     left alone rather than raising.
UPDATE catalog.show l
SET tmdb_id = c.tmdb_id, match_method = c.loser_method
FROM collision c
WHERE l.id = c.loser_id
  AND l.tmdb_id IS NULL
  AND c.loser_method IN ('tvdb_id','imdb_id')
  AND NOT EXISTS (SELECT 1 FROM catalog.show o WHERE o.tmdb_id = c.tmdb_id);

COMMIT;


-- =====================================================================
-- STEP 6 — VERIFY.
-- =====================================================================

-- The worked example: 747 should sit on 4822 via tvdb_id, 4821 back to unmatched.
SELECT id, name, tmdb_id, match_method
FROM catalog.show WHERE id IN (4821, 4822) ORDER BY id;

-- Whole-catalog tier mix. `title_year` falls by the retracted count, the exact
-- tiers rise by the same total, and the unmatched count does not move.
SELECT coalesce(match_method, '(unmatched)') AS tier, count(*)
FROM catalog.show GROUP BY 1 ORDER BY 2 DESC;

-- The acceptance number: user-touched shows, which must be unchanged by all of
-- the above. 2026-08-10: 553 tvdb_id / 6 imdb_id / 4 title_year / 2 unmatched,
-- identical before and after.
SELECT coalesce(c.match_method, '(unmatched)') AS tier, count(*)
FROM catalog.show c
WHERE c.id IN (
    SELECT show_id FROM app.user_show_watch
    UNION SELECT show_id FROM app.user_show_rating
    UNION SELECT e.show_id FROM app.user_episode_watch uew
           JOIN tvmaze.episode e ON e.id = uew.episode_id
    UNION SELECT e.show_id FROM app.user_episode_rating uer
           JOIN tvmaze.episode e ON e.id = uer.episode_id)
GROUP BY 1 ORDER BY 2 DESC;
