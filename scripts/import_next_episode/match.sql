-- Stage 2 of the Next Episode import: resolve titles to shows and marks to
-- episodes. Reads the staging tables loaded by parse_export.py. Writes only
-- to the import_ne schema -- nothing here touches the app schema.
--
-- Run after staging.sql. Safe to re-run.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- Show candidates
--
-- Strict first: exact name, constrained by premiere year when the export
-- supplied one. The AKA fallback runs ONLY for titles that got zero strict
-- hits. Measured on the briggsjm export, letting AKAs compete with strict
-- matches turns clean matches into ambiguous ones -- it drops no-matches from
-- 4.8% to 1.1% but pushes ambiguity from 9.4% to 13.4%. Fallback, not peer.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS import_ne.show_candidate;
CREATE TABLE import_ne.show_candidate AS
SELECT DISTINCT
    s.title,
    sh.id AS show_id,
    'strict'::text AS method,
    sh.name,
    sh.premiered,
    sh.language,
    sh.status
FROM import_ne.series s
JOIN tvmaze.show sh
  ON lower(sh.name) = lower(s.base)
 AND (s.year IS NULL OR extract(year FROM sh.premiered)::int = s.year)

UNION

SELECT DISTINCT
    s.title,
    sh.id,
    'aka'::text,
    sh.name,
    sh.premiered,
    sh.language,
    sh.status
FROM import_ne.series s
JOIN tvmaze.show_aka a ON lower(a.name) = lower(s.base)
JOIN tvmaze.show sh ON sh.id = a.show_id
WHERE (s.year IS NULL OR extract(year FROM sh.premiered)::int = s.year)
  AND NOT EXISTS (
        SELECT 1 FROM tvmaze.show s2
        WHERE lower(s2.name) = lower(s.base)
          AND (s.year IS NULL OR extract(year FROM s2.premiered)::int = s.year)
      );

-- ---------------------------------------------------------------------------
-- Per-title verdict. LEFT JOIN so titles with no candidate at all still land
-- here with n_candidates = 0 rather than vanishing.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS import_ne.show_match;
CREATE TABLE import_ne.show_match AS
SELECT
    s.title,
    count(DISTINCT c.show_id)::int AS n_candidates,
    CASE WHEN count(DISTINCT c.show_id) = 1 THEN min(c.show_id) END AS show_id
FROM import_ne.series s
LEFT JOIN import_ne.show_candidate c USING (title)
GROUP BY s.title;

-- ---------------------------------------------------------------------------
-- show_resolution is the authoritative title -> show_id map, and the one
-- table a human edits. Unambiguous matches are seeded automatically;
-- everything else is filled in from the reviewed CSV.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS import_ne.show_resolution;
CREATE TABLE import_ne.show_resolution (
    title   text PRIMARY KEY REFERENCES import_ne.series(title),
    show_id int NOT NULL REFERENCES tvmaze.show(id),
    source  text NOT NULL DEFAULT 'auto'
);

INSERT INTO import_ne.show_resolution (title, show_id, source)
SELECT title, show_id, 'auto' FROM import_ne.show_match WHERE n_candidates = 1;

-- Everything needing a human decision, with the columns that actually
-- distinguish candidates. Premiere year and language are load-bearing here:
-- "Friends" is four rows and nothing else tells them apart.
CREATE OR REPLACE VIEW import_ne.show_review AS
SELECT
    m.title,
    m.n_candidates,
    c.show_id AS candidate_show_id,
    c.name    AS candidate_name,
    c.premiered,
    c.language,
    c.status,
    c.method,
    NULL::int AS chosen_show_id   -- fill this in, one row per title
FROM import_ne.show_match m
LEFT JOIN import_ne.show_candidate c USING (title)
WHERE m.n_candidates <> 1
ORDER BY m.n_candidates, m.title, c.premiered NULLS LAST;

-- Episode resolution and the run summary live in match_episodes_only.sql.
-- They are split out because this file is destructive: it recreates
-- show_resolution, which would silently discard every manual pick if it were
-- re-run after review. Run this once per export; run the other file freely.
