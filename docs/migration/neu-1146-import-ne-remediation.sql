-- Re-point the two Next Episode staging rows so the orphan shows can be deleted (NEU-1146).
--
-- EXECUTED in production 2026-08-14. Both guards passed, both rows moved, and the
-- re-run that followed deleted the two shows and exited 0. Kept for the record and
-- for the reversal below. It was run as:
--
--   ssh tom@ssh.neuroticsasquat.ch \
--     "docker exec -i $BE psql ..." < docs/migration/neu-1146-import-ne-remediation.sql
--
-- followed by a re-run of the pass, which deleted both shows and exited 0:
-- "no orphan rows remain at any grain — the catalog is TMDB-sourced throughout".
--
-- ## What happened
--
-- The pass ran in production on 2026-08-14 and cleared both lower grains
-- completely: 0 orphan episodes, 0 orphan seasons. It kept two orphan *shows*,
-- and named them:
--
--     kept 2 orphan show(s) still referenced by import_ne staging rows or still
--     holding catalog rows: 63900, 87519
--
-- Both are empty husks. Verified after the run: 0 episodes, 0 seasons, and **0
-- user rows of any kind** — the show-grain links moved every `user_show_watch`
-- onto the linked show (1067768 and 1202502 each now carry one). The only thing
-- holding them is one `import_ne.show_resolution` row apiece:
--
--     title           | show_id | source
--     Cunk On Earth   |   63900 | auto
--     Discretion      |   87519 | auto
--
-- `show_resolution_show_id_fkey` is the **only NO ACTION foreign key into
-- `catalog.show`** — the other 23 all CASCADE — so those two rows are the entire
-- residue. `show_id` is NOT NULL, so nulling is not available: it is re-point or
-- delete.
--
-- ## Why re-point rather than delete
--
-- The row records that the Next Episode import auto-resolved a title to a
-- catalog show. That resolution is still true; only the show representing it has
-- changed. TMDB models "Cunk on Earth" as **season 2 of "Cunk on..."** (show
-- 1067768) and holds "Discretion" as an ordinary series (1202502), and the pass
-- has already moved every episode and every user row onto those shows. Deleting
-- the staging rows would lose the record that those two NE titles were ever
-- resolved; re-pointing keeps it and makes it accurate.
--
-- These are the same two shows NEU-1066 hand-treated for the same underlying
-- reason, and its remediation file is the precedent for the shape of this one.
--
-- ## Why the pass does not do this itself
--
-- `orphan_retire` deliberately refuses to write to `import_ne`. The staging
-- tables are an import audit trail kept for re-runs, they are not part of the
-- running app, and a migration pass silently rewriting another subsystem's
-- records is not a default anyone should get without asking for it — so the pass
-- skips such a show, reports it in `shows_kept_referenced`, and exits 1 so the
-- residue cannot be mistaken for success.
--
-- That is the right default and it should stay. Two rows, resolved by hand, in
-- the open, is the correct amount of ceremony for a decision about a schema this
-- ticket does not own.
--
-- ## Reversal
--
-- Exactly reversible; the original values are the ones named here:
--
--   UPDATE import_ne.show_resolution SET show_id = 63900 WHERE show_id = 1067768;
--   UPDATE import_ne.show_resolution SET show_id = 87519 WHERE show_id = 1202502;
--
-- Reversing it after the shows are deleted would raise, the referents being gone
-- — which is the point at which this stops being reversible and the pre-drop
-- `tvmaze` dump becomes the only route back.

BEGIN;

-- Guard: refuse unless the destinations are the shows we verified, still hold
-- their TMDB ids, and the sources are the empty husks the pass left behind.
-- A mismatch here means the database is not in the state this file was written
-- against, and the right response is to look rather than to force it.
DO $$
DECLARE
    ok boolean;
BEGIN
    SELECT
        (SELECT count(*) FROM catalog.show WHERE id = 1067768 AND tmdb_id = 79063) = 1
    AND (SELECT count(*) FROM catalog.show WHERE id = 1202502 AND tmdb_id = 300966) = 1
    AND (SELECT count(*) FROM catalog.show WHERE id IN (63900, 87519) AND tmdb_id IS NULL) = 2
    AND (SELECT count(*) FROM catalog.episode WHERE show_id IN (63900, 87519)) = 0
    AND (SELECT count(*) FROM catalog.season WHERE show_id IN (63900, 87519)) = 0
    AND (SELECT count(*) FROM app.user_show_watch WHERE show_id IN (63900, 87519)) = 0
    AND (SELECT count(*) FROM app.user_show_rating WHERE show_id IN (63900, 87519)) = 0
    AND (SELECT count(*) FROM app.activity_event
          WHERE target_type = 'show' AND target_id IN (63900, 87519)) = 0
    INTO ok;

    IF NOT ok THEN
        RAISE EXCEPTION
            'preconditions not met: expected 63900 and 87519 to be empty, user-row-free '
            'orphan shows and 1067768/1202502 to be their ingested counterparts';
    END IF;
END $$;

UPDATE import_ne.show_resolution SET show_id = 1067768 WHERE show_id = 63900;
UPDATE import_ne.show_resolution SET show_id = 1202502 WHERE show_id = 87519;

-- Both rows moved, and nothing now references the two husks.
DO $$
DECLARE
    remaining integer;
BEGIN
    SELECT count(*) INTO remaining
      FROM import_ne.show_resolution WHERE show_id IN (63900, 87519);
    IF remaining <> 0 THEN
        RAISE EXCEPTION '% staging row(s) still reference the orphan shows', remaining;
    END IF;
END $$;

COMMIT;
