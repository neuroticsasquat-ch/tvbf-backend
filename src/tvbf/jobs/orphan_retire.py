"""Retire the TV Maze orphan rows, and its report, as a CLI (NEU-1146).

    python -m tvbf.jobs.orphan_retire report
    python -m tvbf.jobs.orphan_retire retire [--limit N]

A CLI for the same reason `episode_repoint` and `season_dedupe` are: a
migration-window operation run by hand a handful of times, with no cursor to
advance and nothing to poll. The process *is* the run, so the exit code is the
result. `tvbf.tmdb.orphan_retire` holds the matcher, the pass and the report;
this is argparse over them. It needs no TMDB credential — every question here is
answered in Postgres.

**Run `report` first and read it.** This is the pass that ends the migration's
absolute constraint. It deletes user watch records — ~95 of them against
production on 2026-08-14 — for content TMDB does not model as an episode of that
series, and `report` prints every one of those rows *before* they are deleted,
split into the ones a surviving twin already records (a de-duplication, no loss)
and the ones with no counterpart at all (a genuine loss). Diff that list against
the spec's §6 and commit the run's own copy; it is the artifact of record, not
the figures in the spec, which move as deltas land.

**It writes to `app`, and it creates rows there as well as moving them.** History
re-pointed into a *different* show gets that show added to the user's My Shows,
or the rows are intact by count and invisible in the product. That is an expected
reconciliation **gain**, as the deletions are expected losses — `task
reconcile:verify` will not come back clean after this pass, by design, and its
three expected discrepancy classes are enumerated in the spec's §7 and in
`docs/migration/README.md`.

**This is not reversible.** The pre-drop `tvmaze` dump is the only source for the
deleted catalog rows and it cannot restore `app` rows at all. `app.watch_archive`
is what can: a human-readable snapshot of every watch and rating, no foreign
keys, survives everything.

**Exit codes: 0 = no orphan row survives, 1 = it aborted, raised, refused to run
before the full TMDB ingest, or left orphans behind.** Unlike its siblings, rows
left behind *are* a failure here: criterion 7 is that all three tables hold zero
`tmdb_id IS NULL` rows afterwards, and the residue **is** the TV Maze data the
ticket exists to remove. Exiting 0 on a partial run would let it read as a
completed migration. Under `--limit` residue is expected and the exit stays 0.

**`report` writes JSON to stdout and nothing else**, like `reconcile capture`,
`human_queue list`, `episode_map report`, `season_dedupe report` and
`episode_repoint report`: `docs/` is not in the production image and a Coolify
container is replaced on every deploy, so the artifact has to travel over
`ssh 'docker exec ...'`. Logs go to stderr.

`--limit N` stops once at least N orphan episodes have been retired, which is
how to try a hundred before spending the whole pass. It rounds up to a show
boundary — a show is one transaction — so a limit of 100 that lands on Saturday
Night Live retires all 89 of its orphans. It deliberately **skips the season and
show phases** — a season is only deletable once its episodes are gone, and a partial
episode pass has not established that for any show it did not reach.
"""

import argparse
import asyncio
import json
import logging
import sys

from tvbf.config import get_settings
from tvbf.db import SessionLocal
from tvbf.logging_config import configure_logging
from tvbf.tmdb.orphan_retire import (
    LOSS_DEDUPLICATION,
    LOSS_GENUINE,
    MIN_INGESTED_SHOWS,
    IngestNotRun,
    build_report,
    retire_orphans,
)

log = logging.getLogger(__name__)


async def _retire(limit: int | None) -> int:
    async with SessionLocal() as session:
        try:
            result = await retire_orphans(session, limit=limit)
        except IngestNotRun as exc:
            # A readable line rather than a stack trace: the operator's next move
            # is to run the ingest, not to read a traceback.
            log.error("%s", exc)
            return 1
        report = await build_report(session)

    log.info(
        "deleted %d orphan episode(s) across %d show(s); moved %d watch(es), %d rating(s) "
        "and %d activity event(s)",
        result.episodes_deleted,
        result.shows_planned,
        result.watches_moved,
        result.ratings_moved,
        result.activity_moved,
    )
    if result.watches_deleted or result.ratings_deleted:
        # The accepted loss, said out loud. It is the whole reason `report` exists
        # and the reason this pass needed an ADR.
        log.warning(
            "deleted %d watch row(s), %d rating(s) and %d activity event(s) that had no "
            "TMDB counterpart to move to — recoverable by hand from app.watch_archive",
            result.watches_deleted,
            result.ratings_deleted,
            result.activity_deleted,
        )
    if result.show_watches_created:
        log.info(
            "added %d show(s) to a user's My Shows, where their history moved to a series "
            "they did not track",
            result.show_watches_created,
        )
    log.info(
        "deleted %d orphan season(s) (%d episode(s) re-pointed onto the ingested season, "
        "%d left without one) and %d orphan show(s)",
        result.seasons_deleted,
        result.episodes_repointed_to_ingested_season,
        result.episodes_left_without_season,
        result.shows_deleted,
    )
    if result.shows_kept_referenced:
        # Cannot be deleted rather than should not be: `import_ne.show_resolution`
        # references `catalog.show` with NO ACTION, and those staging rows are an
        # import audit trail rather than ours to rewrite.
        log.warning(
            "kept %d orphan show(s) still referenced by import_ne staging rows or still "
            "holding catalog rows: %s",
            len(result.shows_kept_referenced),
            ", ".join(str(show_id) for show_id in result.shows_kept_referenced),
        )

    if result.episodes_left_without_season:
        # An ingested episode whose season row went with no ingested season of
        # that number to inherit it. `season_id` is nullable and the read paths
        # key on `(show_id, season_number)`, which the episode still carries, so
        # this is survivable — but it is a TMDB-sourced row losing a pointer and
        # it is never silent.
        log.warning(
            "%d ingested episode(s) were left with no season row: their orphan season had no "
            "ingested counterpart of that number to inherit them",
            result.episodes_left_without_season,
        )

    remaining = report.orphan_episodes + report.orphan_seasons + report.orphan_shows
    if remaining and limit is not None:
        log.info(
            "%d orphan row(s) remain, as expected under --limit — re-run without it to finish",
            remaining,
        )
        return 0
    if remaining:
        # Criterion 7, and the exit code is how it is scored. Every sibling pass
        # treats residue as its expected output; this one must not, because the
        # residue *is* the TV Maze data the ticket exists to remove and the
        # frontend half is gated on it reaching zero. Exiting 0 here would let a
        # partial run read as a completed migration.
        log.error(
            "%d orphan row(s) remain — %d episode(s), %d season(s), %d show(s). Criterion 7 is "
            "not met: the TVmaze credit stays in the footer and the frontend half must not land",
            remaining,
            report.orphan_episodes,
            report.orphan_seasons,
            report.orphan_shows,
        )
        return 1
    log.info("no orphan rows remain at any grain — the catalog is TMDB-sourced throughout")
    return 0


async def _report() -> int:
    async with SessionLocal() as session:
        report = await build_report(session)

    # The artifact, and nothing else, on stdout.
    sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")

    log.info(
        "%d orphan episode(s), %d season(s), %d show(s); %d episode(s) would be re-pointed "
        "and %d deleted",
        report.orphan_episodes,
        report.orphan_seasons,
        report.orphan_shows,
        sum(report.by_tier.values()),
        report.to_delete,
    )
    log.info(
        "by tier: %s",
        ", ".join(f"{tier}={count}" for tier, count in report.by_tier.items()),
    )
    log.info(
        "user-touched: %d orphan(s) move, %d are deleted",
        sum(report.by_tier_user_touched.values()),
        report.to_delete_user_touched,
    )
    log.info(
        "%d show link(s) proposed, %d dropped for carrying more than one candidate; "
        "%d link(s) carry user data and are the ones to review",
        len(report.links),
        report.links_dropped_multiple_candidates,
        sum(1 for link in report.links if link["user_touched"]),
    )
    genuine = report.loss_summary.get(LOSS_GENUINE, 0)
    deduped = report.loss_summary.get(LOSS_DEDUPLICATION, 0)
    if genuine or deduped:
        # Loud, and split: folding the two together over-reports the loss by the
        # count of two-parter halves a surviving twin already records.
        log.warning(
            "%d user row(s) would be deleted rather than moved: %d a genuine loss, %d a "
            "de-duplication a surviving twin already records. Read `losses` before running "
            "`retire` — this is the list to review and commit",
            genuine + deduped,
            genuine,
            deduped,
        )
    if report.show_watches_to_create:
        log.info(
            "%d My Shows row(s) would be created for history moving to a series the user "
            "does not track — an expected reconciliation gain",
            report.show_watches_to_create,
        )
    if report.ingested_shows < MIN_INGESTED_SHOWS:
        # `report` deliberately carries no floor — it is the half you run to
        # decide, including on a database where the pass would refuse. But a
        # pre-ingest reading makes almost every orphan look like a deletion,
        # which is the exact misread `IngestNotRun` exists to prevent.
        log.warning(
            "only %d show(s) carry a tmdb_synced_at, under the floor of %d — these counts "
            "read as near-total deletion because the ingest has not run, not because the "
            "orphans have no counterparts; `retire` will refuse until it has",
            report.ingested_shows,
            MIN_INGESTED_SHOWS,
        )
    return 0


async def run(args: argparse.Namespace) -> int:
    """The whole job, minus argument parsing. Returns the process exit code.

    Split out from `main` so tests can await it on their own event loop —
    `main`'s `asyncio.run` would otherwise rebuild the loop under the shared
    engine's pooled connections.
    """
    if args.mode == "retire":
        return await _retire(args.limit)
    return await _report()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tvbf.jobs.orphan_retire")
    modes = parser.add_subparsers(dest="mode", required=True)

    modes.add_parser(
        "report",
        help="what the pass would do, including the user rows it would delete (JSON)",
    )
    retire = modes.add_parser(
        "retire",
        help="re-point every orphan with a TMDB counterpart, delete the rest",
    )
    retire.add_argument(
        "--limit",
        type=int,
        help=(
            "stop after retiring at least this many orphan episodes (rounds up to a "
            "show boundary); skips the season and show phases"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        return asyncio.run(run(args))
    except Exception:
        log.exception("orphan retirement failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
