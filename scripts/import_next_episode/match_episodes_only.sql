-- Episode resolution + run summary for the Next Episode import.
--
-- Deliberately separate from match.sql, which recreates show_resolution and
-- would therefore wipe every manual pick. This file only reads that table, so
-- it is safe to re-run as often as you like -- after each review pass, or just
-- to look at where things stand.

\set ON_ERROR_STOP on

-- Only covers titles present in show_resolution, so hand-resolved shows get
-- picked up automatically on each re-run.
DROP TABLE IF EXISTS import_ne.episode_match;
CREATE TABLE import_ne.episode_match AS
SELECT
    em.title,
    em.raw,
    em.season,
    em.number,
    r.show_id,
    e.id AS episode_id,
    CASE
        WHEN em.number IS NULL THEN 'special-unnumbered'
        WHEN e.id IS NULL      THEN 'no-such-episode'
        ELSE 'ok'
    END AS outcome
FROM import_ne.episode_mark em
JOIN import_ne.show_resolution r USING (title)
LEFT JOIN tvmaze.episode e
       ON e.show_id = r.show_id
      AND e.season  = em.season
      AND e.number  = em.number
      AND em.number IS NOT NULL;

CREATE INDEX ON import_ne.episode_match (outcome);

\echo ''
\echo '=== SHOWS ==='
SELECT
    CASE WHEN r.title IS NOT NULL AND r.source = 'manual' THEN 'resolved (manual)'
         WHEN r.title IS NOT NULL                         THEN 'resolved (auto)'
         WHEN m.n_candidates = 0                          THEN 'no candidate  -- needs manual search'
         ELSE                                                  'ambiguous     -- needs a pick' END AS outcome,
    count(*),
    round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM import_ne.show_match m
LEFT JOIN import_ne.show_resolution r USING (title)
GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '=== EPISODE MARKS (resolved shows only) ==='
SELECT outcome, count(*),
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM import_ne.episode_match
GROUP BY 1 ORDER BY 2 DESC;

\echo ''
\echo '=== marks stranded on still-unresolved shows ==='
SELECT count(*) AS stranded_marks
FROM import_ne.episode_mark em
WHERE NOT EXISTS (SELECT 1 FROM import_ne.show_resolution r WHERE r.title = em.title);

\echo ''
\echo '=== still needing a human (title, candidates) ==='
SELECT m.title, m.n_candidates
FROM import_ne.show_match m
LEFT JOIN import_ne.show_resolution r USING (title)
WHERE r.title IS NULL
ORDER BY m.n_candidates, m.title;
