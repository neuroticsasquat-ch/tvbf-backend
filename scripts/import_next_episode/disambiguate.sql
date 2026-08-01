-- Auto-disambiguate ambiguous titles using the user's own watch marks.
--
-- When a title matches several catalog rows, the export usually already
-- contains enough evidence to tell them apart: the episodes the user says
-- they watched have to actually exist on the right show. Only one "Friends"
-- ran ten seasons; only one "The Office" has a 9x23. Checking coverage turns
-- a large share of the ambiguous pile into decisions nobody has to make.
--
-- Conservative by construction. A candidate wins only if it covers strictly
-- more marks than every rival -- ties, zero-coverage titles, and titles with
-- no marks at all are left for a human. Re-runnable; only ever inserts
-- resolutions for titles that do not have one yet.

\set ON_ERROR_STOP on

DROP TABLE IF EXISTS import_ne.coverage;
CREATE TABLE import_ne.coverage AS
WITH unresolved AS (
    SELECT m.title
    FROM import_ne.show_match m
    LEFT JOIN import_ne.show_resolution r USING (title)
    WHERE r.title IS NULL
)
SELECT
    c.title,
    c.show_id,
    c.name,
    c.premiered,
    c.language,
    -- how many of this title's numbered marks exist on this candidate
    count(e.id)::int AS marks_covered,
    (SELECT count(*) FROM import_ne.episode_mark em2
      WHERE em2.title = c.title AND em2.number IS NOT NULL)::int AS marks_total
FROM unresolved u
JOIN import_ne.show_candidate c USING (title)
LEFT JOIN import_ne.episode_mark em
       ON em.title = c.title AND em.number IS NOT NULL
LEFT JOIN tvmaze.episode e
       ON e.show_id = c.show_id AND e.season = em.season AND e.number = em.number
GROUP BY c.title, c.show_id, c.name, c.premiered, c.language;

-- A clear winner: best coverage, nonzero, and strictly ahead of second place.
DROP TABLE IF EXISTS import_ne.coverage_winner;
CREATE TABLE import_ne.coverage_winner AS
WITH ranked AS (
    SELECT *,
           row_number() OVER (PARTITION BY title ORDER BY marks_covered DESC) AS rn,
           count(*)     OVER (PARTITION BY title, marks_covered)              AS ties_at_this_level,
           max(marks_covered) OVER (PARTITION BY title)                       AS best
    FROM import_ne.coverage
)
SELECT title, show_id, name, premiered, language, marks_covered, marks_total
FROM ranked
WHERE rn = 1
  AND marks_covered > 0
  AND marks_covered = best
  AND ties_at_this_level = 1;

INSERT INTO import_ne.show_resolution (title, show_id, source)
SELECT title, show_id, 'coverage' FROM import_ne.coverage_winner
ON CONFLICT (title) DO NOTHING;

\echo ''
\echo '=== auto-resolved by watch-mark coverage ==='
SELECT title, name, premiered, language,
       marks_covered || '/' || marks_total AS marks
FROM import_ne.coverage_winner
ORDER BY marks_total DESC, title;
