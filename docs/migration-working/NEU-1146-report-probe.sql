\pset pager off
SET statement_timeout='3000s';
CREATE OR REPLACE FUNCTION pg_temp.fold(t text) RETURNS text LANGUAGE sql IMMUTABLE AS
$$ SELECT public.immutable_unaccent(lower(regexp_replace(coalesce(t,''),'[[:punct:][:space:]]+','','g'))) $$;

CREATE TEMP TABLE o AS
SELECT e.id, e.show_id, e.season_number sn, e.episode_number en, e.name, e.air_date,
       pg_temp.fold(e.name) fn, s.name shw, pg_temp.fold(s.name) sfn,
       (EXISTS(SELECT 1 FROM app.user_episode_watch w WHERE w.episode_id=e.id)
        OR EXISTS(SELECT 1 FROM app.user_episode_rating r WHERE r.episode_id=e.id)) touched
FROM catalog.episode e JOIN catalog.show s ON s.id=e.show_id
WHERE e.tmdb_id IS NULL AND s.tmdb_id IS NOT NULL;
CREATE INDEX ON o(show_id, fn); CREATE INDEX ON o(show_id, sn, en); CREATE INDEX ON o(id);

CREATE TEMP TABLE ing AS
SELECT e.id, e.show_id, e.season_number sn, e.episode_number en, pg_temp.fold(e.name) fn, e.air_date
FROM catalog.episode e WHERE e.tmdb_id IS NOT NULL AND e.show_id IN (SELECT DISTINCT show_id FROM o);
CREATE INDEX ON ing(show_id, fn); CREATE INDEX ON ing(show_id, sn, en);
ANALYZE o; ANALYZE ing;

-- tier 0
CREATE TEMP TABLE t0 AS
SELECT o.id AS orphan_id FROM o JOIN ing i ON i.show_id=o.show_id AND i.sn=o.sn AND i.en=o.en
WHERE (SELECT count(*) FROM ing x WHERE x.show_id=o.show_id AND x.sn=o.sn AND x.en=o.en)=1
  AND (SELECT count(*) FROM o y WHERE y.show_id=o.show_id AND y.sn=o.sn AND y.en=o.en)=1;
CREATE INDEX ON t0(orphan_id); ANALYZE t0;

-- tier 1
CREATE TEMP TABLE oc AS SELECT show_id, fn, count(*) n, min(id) id FROM o WHERE fn<>'' GROUP BY show_id, fn;
CREATE TEMP TABLE ic AS SELECT show_id, fn, count(*) n, min(id) id FROM ing WHERE fn<>'' GROUP BY show_id, fn;
CREATE INDEX ON oc(show_id,fn); CREATE INDEX ON ic(show_id,fn); ANALYZE oc; ANALYZE ic;
CREATE TEMP TABLE t1 AS
SELECT oc.id AS orphan_id FROM oc JOIN ic ON ic.show_id=oc.show_id AND ic.fn=oc.fn
WHERE oc.n=1 AND ic.n=1 AND NOT EXISTS (SELECT 1 FROM t0 WHERE t0.orphan_id=oc.id);
CREATE INDEX ON t1(orphan_id); ANALYZE t1;

-- tier 2 step 1: link candidates via same-folded-name sibling + title/date evidence
CREATE TEMP TABLE sib AS
SELECT DISTINCT a.id AS orphan_show, b.id AS sib_show
FROM (SELECT DISTINCT show_id, sfn FROM o) a2 JOIN catalog.show a ON a.id=a2.show_id
JOIN catalog.show b ON b.id<>a.id AND b.tmdb_id IS NOT NULL AND pg_temp.fold(b.name)=a2.sfn;
CREATE INDEX ON sib(orphan_show); ANALYZE sib;

CREATE TEMP TABLE sibep AS
SELECT e.id, e.show_id, e.season_number sn, e.episode_number en, pg_temp.fold(e.name) fn, e.air_date
FROM catalog.episode e WHERE e.tmdb_id IS NOT NULL AND e.show_id IN (SELECT DISTINCT sib_show FROM sib);
CREATE INDEX ON sibep(show_id, fn, air_date); CREATE INDEX ON sibep(show_id, sn, en); ANALYZE sibep;

CREATE TEMP TABLE linkcand AS
SELECT o.show_id AS orphan_show, se.show_id AS sib_show, count(*) AS evidence
FROM o JOIN sib sb ON sb.orphan_show=o.show_id
JOIN sibep se ON se.show_id=sb.sib_show AND se.fn=o.fn AND se.air_date=o.air_date
WHERE o.fn<>'' AND o.air_date IS NOT NULL
GROUP BY 1,2;
CREATE INDEX ON linkcand(orphan_show); ANALYZE linkcand;

-- exactly one candidate show per orphan show
CREATE TEMP TABLE link AS
SELECT l.orphan_show, l.sib_show, l.evidence FROM linkcand l
WHERE (SELECT count(*) FROM linkcand l2 WHERE l2.orphan_show=l.orphan_show)=1;
CREATE INDEX ON link(orphan_show); ANALYZE link;

-- step 2: modal season offset from the evidence pairs
CREATE TEMP TABLE offc AS
SELECT l.orphan_show, l.sib_show, (o.sn - se.sn) AS off, count(*) AS n
FROM link l JOIN o ON o.show_id=l.orphan_show
JOIN sibep se ON se.show_id=l.sib_show AND se.fn=o.fn AND se.air_date=o.air_date
WHERE o.fn<>'' AND o.air_date IS NOT NULL
GROUP BY 1,2,3;
CREATE TEMP TABLE off AS
SELECT DISTINCT ON (orphan_show) orphan_show, sib_show, off, n FROM offc ORDER BY orphan_show, n DESC, off;
CREATE INDEX ON off(orphan_show); ANALYZE off;

-- step 3: pair on (season - offset, episode_number), 1:1 both sides
CREATE TEMP TABLE t2 AS
SELECT o.id AS orphan_id, f.sib_show AS twin_show
FROM o JOIN off f ON f.orphan_show=o.show_id
WHERE NOT EXISTS (SELECT 1 FROM t0 WHERE t0.orphan_id=o.id)
  AND NOT EXISTS (SELECT 1 FROM t1 WHERE t1.orphan_id=o.id)
  AND (SELECT count(*) FROM sibep se WHERE se.show_id=f.sib_show AND se.sn=o.sn-f.off AND se.en=o.en)=1
  AND (SELECT count(*) FROM o o2 WHERE o2.show_id=o.show_id AND o2.sn=o.sn AND o2.en=o.en)=1;
CREATE INDEX ON t2(orphan_id); ANALYZE t2;

-- tier 2b: title fallback within a linked pair where the offset placed nothing
CREATE TEMP TABLE t2b AS
SELECT o.id AS orphan_id, l.sib_show AS twin_show
FROM o JOIN link l ON l.orphan_show=o.show_id
WHERE o.fn<>'' AND o.air_date IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM t0 WHERE t0.orphan_id=o.id)
  AND NOT EXISTS (SELECT 1 FROM t1 WHERE t1.orphan_id=o.id)
  AND NOT EXISTS (SELECT 1 FROM t2 WHERE t2.orphan_id=o.id)
  AND (SELECT count(*) FROM sibep se WHERE se.show_id=l.sib_show AND se.fn=o.fn AND se.air_date=o.air_date)=1
  AND (SELECT count(*) FROM o o2 WHERE o2.show_id=o.show_id AND o2.fn=o.fn AND o2.air_date=o.air_date)=1;
CREATE INDEX ON t2b(orphan_id); ANALYZE t2b;

\echo '=========== NEU-1146 REPORT (offset rule) ==========='
\echo '--- 1. tier dispositions ---'
SELECT 'total_orphans_under_matched_show' k, count(*)::text v FROM o
UNION ALL SELECT 'tier0_exact_key', (SELECT count(*)::text FROM t0)
UNION ALL SELECT 'tier1_same_show_unique_title', (SELECT count(*)::text FROM t1)
UNION ALL SELECT 'tier2_offset_key', (SELECT count(*)::text FROM t2)
UNION ALL SELECT 'tier2b_title_fallback', (SELECT count(*)::text FROM t2b)
UNION ALL SELECT 'tier3_DELETE', (SELECT count(*)::text FROM o WHERE NOT EXISTS(SELECT 1 FROM t0 WHERE t0.orphan_id=o.id)
   AND NOT EXISTS(SELECT 1 FROM t1 WHERE t1.orphan_id=o.id) AND NOT EXISTS(SELECT 1 FROM t2 WHERE t2.orphan_id=o.id)
   AND NOT EXISTS(SELECT 1 FROM t2b WHERE t2b.orphan_id=o.id));

\echo '--- 2. links: candidates, accepted, offsets ---'
SELECT 'orphan_shows_with_link_candidates' k, count(DISTINCT orphan_show)::text v FROM linkcand
UNION ALL SELECT 'links_accepted_exactly_one_candidate', (SELECT count(*)::text FROM link)
UNION ALL SELECT 'links_dropped_multiple_candidates', (SELECT count(DISTINCT orphan_show)::text FROM linkcand l
   WHERE (SELECT count(*) FROM linkcand l2 WHERE l2.orphan_show=l.orphan_show)>1)
UNION ALL SELECT 'links_with_offset', (SELECT count(*)::text FROM off);

\echo '--- 3. user-touched dispositions ---'
SELECT 'user_touched_total' k, count(*)::text v FROM o WHERE touched
UNION ALL SELECT 'moved_tier0', (SELECT count(*)::text FROM o JOIN t0 ON t0.orphan_id=o.id WHERE o.touched)
UNION ALL SELECT 'moved_tier1', (SELECT count(*)::text FROM o JOIN t1 ON t1.orphan_id=o.id WHERE o.touched)
UNION ALL SELECT 'moved_tier2_offset', (SELECT count(*)::text FROM o JOIN t2 ON t2.orphan_id=o.id WHERE o.touched)
UNION ALL SELECT 'moved_tier2b_title', (SELECT count(*)::text FROM o JOIN t2b ON t2b.orphan_id=o.id WHERE o.touched)
UNION ALL SELECT 'DELETED', (SELECT count(*)::text FROM o WHERE o.touched AND NOT EXISTS(SELECT 1 FROM t0 WHERE t0.orphan_id=o.id)
   AND NOT EXISTS(SELECT 1 FROM t1 WHERE t1.orphan_id=o.id) AND NOT EXISTS(SELECT 1 FROM t2 WHERE t2.orphan_id=o.id)
   AND NOT EXISTS(SELECT 1 FROM t2b WHERE t2b.orphan_id=o.id));

\echo '--- 4. row-grain totals ---'
SELECT 'watch_rows_moved' k, (SELECT count(*)::text FROM app.user_episode_watch w WHERE EXISTS(SELECT 1 FROM t0 WHERE t0.orphan_id=w.episode_id)
   OR EXISTS(SELECT 1 FROM t1 WHERE t1.orphan_id=w.episode_id) OR EXISTS(SELECT 1 FROM t2 WHERE t2.orphan_id=w.episode_id)
   OR EXISTS(SELECT 1 FROM t2b WHERE t2b.orphan_id=w.episode_id)) v
UNION ALL SELECT 'watch_rows_deleted', (SELECT count(*)::text FROM o JOIN app.user_episode_watch w ON w.episode_id=o.id
   WHERE NOT EXISTS(SELECT 1 FROM t0 WHERE t0.orphan_id=o.id) AND NOT EXISTS(SELECT 1 FROM t1 WHERE t1.orphan_id=o.id)
     AND NOT EXISTS(SELECT 1 FROM t2 WHERE t2.orphan_id=o.id) AND NOT EXISTS(SELECT 1 FROM t2b WHERE t2b.orphan_id=o.id))
UNION ALL SELECT 'rating_rows_deleted', (SELECT count(*)::text FROM o JOIN app.user_episode_rating r ON r.episode_id=o.id
   WHERE NOT EXISTS(SELECT 1 FROM t0 WHERE t0.orphan_id=o.id) AND NOT EXISTS(SELECT 1 FROM t1 WHERE t1.orphan_id=o.id)
     AND NOT EXISTS(SELECT 1 FROM t2 WHERE t2.orphan_id=o.id) AND NOT EXISTS(SELECT 1 FROM t2b WHERE t2b.orphan_id=o.id));

\echo '--- 5. links carrying user data (the ones needing review) ---'
SELECT f.orphan_show, (SELECT name FROM catalog.show WHERE id=f.orphan_show) AS from_name,
       f.sib_show AS to_show, (SELECT name FROM catalog.show WHERE id=f.sib_show) AS to_name,
       f.off AS season_offset,
       (SELECT count(*) FROM t2 WHERE t2.twin_show=f.sib_show) AS episodes_moved,
       (SELECT count(*) FROM o JOIN t2 ON t2.orphan_id=o.id WHERE o.show_id=f.orphan_show AND o.touched) AS user_touched
FROM off f
WHERE (SELECT count(*) FROM o JOIN t2 ON t2.orphan_id=o.id WHERE o.show_id=f.orphan_show AND o.touched) > 0
ORDER BY user_touched DESC;

\echo '--- 6. user_show_watch rows created ---'
SELECT t.twin_show AS show_id, (SELECT name FROM catalog.show WHERE id=t.twin_show) AS show_name,
       count(DISTINCT w.user_id) AS users
FROM o JOIN (SELECT orphan_id, twin_show FROM t2 UNION ALL SELECT orphan_id, twin_show FROM t2b) t ON t.orphan_id=o.id
JOIN app.user_episode_watch w ON w.episode_id=o.id
WHERE NOT EXISTS (SELECT 1 FROM app.user_show_watch uw WHERE uw.user_id=w.user_id AND uw.show_id=t.twin_show)
GROUP BY t.twin_show;

\echo '--- 7. LOSS LIST ---'
SELECT o.shw AS show, 's'||o.sn||'e'||o.en AS ep, o.name, o.air_date, count(*) AS watch_rows
FROM o JOIN app.user_episode_watch w ON w.episode_id=o.id
WHERE NOT EXISTS(SELECT 1 FROM t0 WHERE t0.orphan_id=o.id) AND NOT EXISTS(SELECT 1 FROM t1 WHERE t1.orphan_id=o.id)
  AND NOT EXISTS(SELECT 1 FROM t2 WHERE t2.orphan_id=o.id) AND NOT EXISTS(SELECT 1 FROM t2b WHERE t2b.orphan_id=o.id)
GROUP BY o.shw, o.sn, o.en, o.name, o.air_date ORDER BY o.shw, o.sn, o.en;
