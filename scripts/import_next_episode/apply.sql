-- Stage 3 of the Next Episode import: write the resolved ids into the app
-- schema for one user. Reports first, writes only when :apply is 1.
--
-- Required vars:
--   -v user_id='<uuid>'
--   -v watched_at=now|airdate
--   -v apply=0|1
--
-- Writing straight SQL is deliberate, not laziness. app/services/episode_
-- service.py calls activity_service.emit() at three sites; routing thousands
-- of backdated watches through the service layer would fan an event out to
-- every accepted connection's feed, all stamped at import time, with every
-- show landing at once. Going under the service layer means no activity_event
-- rows can be produced at all -- the flood is prevented by construction
-- rather than by remembering to pass a flag.
--
-- ---------------------------------------------------------------------------
-- DO NOT change any ON CONFLICT clause below to DO UPDATE.
--
-- A row the user created by ticking an episode in the app is a real event: it
-- records that a specific person marked a specific episode at a specific
-- moment. A row this script creates is a backfill, and its watched_at is
-- invented -- the export carries no dates, so an airdate is a stand-in for
-- something we do not know.
--
-- Imported data therefore never overwrites tracked data. Where the two
-- collide, the real event wins and the backfill yields. This leaves the
-- occasional timestamp that does not match its airdate; that is correct, not
-- an inconsistency to clean up. Normalizing those rows would replace real
-- history with a fabricated one, and there is no undo.
-- ---------------------------------------------------------------------------

\set ON_ERROR_STOP on

-- Refuse to run against a user id that does not exist here. Without this, a
-- typo'd or wrong-environment uuid silently writes nothing and reports success,
-- which looks identical to "the import had nothing to do".
--
-- The id goes via a GUC because psql does not substitute :vars inside
-- dollar-quoted bodies -- referencing :'user_id' directly in the DO block is a
-- syntax error, not a silently skipped check.
SELECT set_config('import.user_id', :'user_id', false) \gset ignored_
DO $$
BEGIN
  IF NOT EXISTS (
      SELECT 1 FROM app."user" WHERE id = current_setting('import.user_id')::uuid
  ) THEN
    RAISE EXCEPTION 'user % does not exist in this database -- wrong id or wrong environment',
                    current_setting('import.user_id');
  END IF;
END $$;

\echo ''
\echo '=== TARGET USER -- confirm this is the right person before committing ==='
SELECT id, email, display_name, created_at::date AS signed_up
FROM app."user" WHERE id = :'user_id'::uuid;

\echo ''
\echo '=== BEFORE ==='
SELECT
  (SELECT count(*) FROM app.user_show_watch    WHERE user_id = :'user_id') AS my_shows,
  (SELECT count(*) FROM app.user_episode_watch WHERE user_id = :'user_id') AS episode_watches,
  (SELECT count(*) FROM app.activity_event     WHERE actor_id = :'user_id') AS activity_events;

\echo ''
\echo '=== WOULD WRITE ==='
SELECT
  (SELECT count(DISTINCT r.show_id)
     FROM import_ne.show_resolution r
    WHERE NOT EXISTS (SELECT 1 FROM app.user_show_watch w
                       WHERE w.user_id = :'user_id' AND w.show_id = r.show_id)) AS new_my_shows,
  (SELECT count(DISTINCT m.episode_id)
     FROM import_ne.episode_match m
    WHERE m.outcome = 'ok'
      AND NOT EXISTS (SELECT 1 FROM app.user_episode_watch w
                       WHERE w.user_id = :'user_id' AND w.episode_id = m.episode_id)) AS new_episode_watches;

\if :apply

BEGIN;

-- My Shows membership for every resolved title, including the ~69% that
-- carried no episode data at all. Those are membership-only by design.
INSERT INTO app.user_show_watch (user_id, show_id)
SELECT DISTINCT :'user_id'::uuid, r.show_id
FROM import_ne.show_resolution r
ON CONFLICT (user_id, show_id) DO NOTHING;

-- Episode watches. watched_at is NOT NULL DEFAULT now() and the export
-- carries no dates whatsoever, so the timestamp is invented either way --
-- the only choice is which lie is least misleading.
--
--   now            everything stamped at import time. Honest that the date is
--                  unknown, but collapses the whole history into one instant.
--   airdate        the episode's original airdate. Keeps recency ordering
--                  plausible; asserts dates the user never supplied.
--   airdate_floor  airdate, but never earlier than the account's created_at.
--                  Avoids watch records predating the account -- at the cost
--                  of flattening everything older onto the signup date, which
--                  for a back-catalogue-heavy export is nearly all of it.
INSERT INTO app.user_episode_watch (user_id, episode_id, watched_at)
SELECT DISTINCT
    :'user_id'::uuid,
    m.episode_id,
    CASE :'watched_at'
        WHEN 'airdate' THEN coalesce(e.airdate::timestamptz, now())
        WHEN 'airdate_floor' THEN greatest(
                coalesce(e.airdate::timestamptz, now()),
                (SELECT created_at FROM app."user" WHERE id = :'user_id'::uuid))
        ELSE now()
    END
FROM import_ne.episode_match m
JOIN tvmaze.episode e ON e.id = m.episode_id
WHERE m.outcome = 'ok'
ON CONFLICT (user_id, episode_id) DO NOTHING;

COMMIT;

\echo ''
\echo '=== AFTER ==='
SELECT
  (SELECT count(*) FROM app.user_show_watch    WHERE user_id = :'user_id') AS my_shows,
  (SELECT count(*) FROM app.user_episode_watch WHERE user_id = :'user_id') AS episode_watches,
  (SELECT count(*) FROM app.activity_event     WHERE actor_id = :'user_id') AS activity_events;

\echo ''
\echo '=== activity_event must be unchanged; anything created in the last hour is a leak ==='
SELECT count(*) AS recent_activity_events
FROM app.activity_event
WHERE actor_id = :'user_id' AND created_at > now() - interval '1 hour';

\else
\echo ''
\echo '(dry run -- nothing written. re-run with -v apply=1 to commit.)'
\endif
