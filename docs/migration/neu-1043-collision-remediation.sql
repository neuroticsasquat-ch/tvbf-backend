-- Give back the tmdb_ids a tier-3 guess took from an exact match (NEU-1065).
--
-- Run ONCE, after the NEU-1043 enrichment pass finishes, before the NEU-1034
-- ingest. NEU-1065 fixes the cause in code; this repairs the data the first
-- production run produced.
--
-- ## What it repairs
--
-- NEU-1043 orders the mapping tiers correctly *within* a show but not *between*
-- shows: candidates stream in `ORDER BY id`, and the first row to claim a
-- `tmdb_id` keeps it. So a tier-3 title guess on a low-id row can beat a tier-1
-- exact `/find` hit on a higher-id row. Observed in production 2026-08-10:
--
--   catalog show 4822 matched TMDB 747 via tvdb_id, but another catalog row
--   already holds it — left unmatched for the human queue
--
--   4821  Lads Army      tvdb 141551            2002   won 747 via title_year
--   4822  Bad Lads Army   tvdb 85570  tt0417301  2004   lost, had an exact hit
--
-- TMDB's own external-id link says 747 <-> tvdb 85570 <-> Bad Lads Army, so the
-- surviving guess is very probably a false positive. It survived only because
-- 4821 < 4822.
--
-- ## Step 1 — build the VALUES list (this is not SQL, and it is why this file
-- ## cannot be run as-is)
--
-- The collisions exist ONLY in the run's log. The database records that a row is
-- unmatched, never that it lost a contest or to whom — so the facts have to be
-- lifted out of the log before any of the SQL below means anything. If the log
-- is gone, so is the input, and the only remaining route is re-running
-- enrichment and capturing the warnings.
--
--   docker exec <tvbf-backend-container> \
--     grep 'already holds it' /tmp/enrich.log \
--     | sed -E "s/.*catalog show ([0-9]+) matched TMDB ([0-9]+) via ([a-z_]+),.*/        (\1, \2, '\3'::text),/" \
--     | sed '$ s/,$//'
--
-- Paste the output into all three <<< COLLISIONS >>> markers below.
--
-- ## What this does NOT repair
--
-- A tier-3 false positive that contested nothing — a guess onto a TMDB id no
-- other row claims — is invisible to both the log and the database. Those
-- surface only by reading `match_method = 'title_year'` rows by hand. This
-- script is not a substitute for that check.


-- =====================================================================
-- STEP 2 — INSPECT. Read the verdicts before changing anything.
-- =====================================================================
WITH collision(loser_id, tmdb_id, loser_method) AS (
    VALUES                                             -- <<< COLLISIONS >>>
        (4822, 747, 'tvdb_id'::text)
)
SELECT
    c.tmdb_id,
    c.loser_id,        l.name AS loser_name,        c.loser_method AS loser_tier,
    w.id AS winner_id, w.name AS winner_name,       w.match_method AS winner_tier,
    CASE
        WHEN w.id IS NULL
            THEN 'leave — nothing holds this id any more'
        WHEN w.match_method = 'title_year' AND c.loser_method IN ('tvdb_id', 'imdb_id')
            THEN 'RETRACT — a guess is sitting on an exact match''s id'
        WHEN w.match_method IN ('tvdb_id', 'imdb_id') AND c.loser_method IN ('tvdb_id', 'imdb_id')
            THEN 'leave — two exact claims, a real upstream conflict (NEU-1044)'
        ELSE 'leave — the exact match already won'
    END AS verdict
FROM collision c
JOIN catalog.show l ON l.id = c.loser_id
LEFT JOIN catalog.show w ON w.tmdb_id = c.tmdb_id
ORDER BY verdict DESC, c.tmdb_id;

-- Only `RETRACT` rows are touched below. Two exact claims on one TMDB id is a
-- genuine upstream conflict: retracting there would be picking a winner by hand
-- on no better evidence than the job had, which is NEU-1044's job, not this
-- script's.


-- =====================================================================
-- STEP 3 — FIX. Two statements, one transaction, in this order.
--
-- Deliberately not one statement: `uq_show_tmdb_id` is not deferrable, and a
-- single command that frees an id and re-takes it can trip the unique index
-- part-way through.
-- =====================================================================
BEGIN;

-- 3a. Retract the suspect guess. It reverts to `tmdb_id IS NULL`, which is a
--     legitimate terminal state (ADR-0008), not a broken row.
WITH collision(loser_id, tmdb_id, loser_method) AS (
    VALUES                                             -- <<< COLLISIONS >>>
        (4822, 747, 'tvdb_id'::text)
)
UPDATE catalog.show w
SET tmdb_id = NULL, match_method = NULL
FROM collision c
WHERE w.tmdb_id = c.tmdb_id
  AND w.match_method = 'title_year'
  AND c.loser_method IN ('tvdb_id', 'imdb_id');

-- 3b. Hand the id to the row that matched it exactly. The NOT EXISTS mirrors the
--     job's own guard, so this no-ops rather than raising if 3a matched nothing.
WITH collision(loser_id, tmdb_id, loser_method) AS (
    VALUES                                             -- <<< COLLISIONS >>>
        (4822, 747, 'tvdb_id'::text)
)
UPDATE catalog.show l
SET tmdb_id = c.tmdb_id, match_method = c.loser_method
FROM collision c
WHERE l.id = c.loser_id
  AND l.tmdb_id IS NULL
  AND c.loser_method IN ('tvdb_id', 'imdb_id')
  AND NOT EXISTS (SELECT 1 FROM catalog.show o WHERE o.tmdb_id = c.tmdb_id);

COMMIT;


-- =====================================================================
-- STEP 4 — VERIFY. The exact tier should hold each contested id.
-- =====================================================================
SELECT id, name, tmdb_id, match_method
FROM catalog.show
WHERE tmdb_id IN (747)                                 -- <<< contested ids >>>
   OR id IN (4821, 4822)                               -- <<< both sides >>>
ORDER BY id;

-- Whole-run tier mix, worth recording in the ticket.
SELECT coalesce(match_method, '(unmatched)') AS tier, count(*)
FROM catalog.show GROUP BY 1 ORDER BY 2 DESC;

-- A follow-up `python -m tvbf.jobs.tmdb_enrichment` is optional and
-- self-correcting: a retracted row is reconsidered, re-finds the same series,
-- and is refused by the NOT EXISTS guard — which is now the right answer. The
-- collision count will therefore not fall to zero, and should not.
