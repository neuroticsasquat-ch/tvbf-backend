"""Proving the airdate correction worked, against a labelled test set (NEU-1145 §7).

Acceptance criteria 2 and 3 are the ticket's real proof, and neither is a unit
test: one is a human reading Apple's published schedule, the other a comparison
against data captured before the cutover. This module builds what both need.

**`app.watch_archive` is the free labelled test set.** It snapshotted every
watch and rating *pre-cutover*, in human terms and with TV Maze's airdate on
each episode row (NEU-1029) — so for 440 episodes somebody actually watched, we
hold the answer TMDB disagreed with. §2.4's whole finding came out of it: 198
Apple TV and 93 Prime Video rows a day early against 1,056 Netflix rows that
agree. Re-running the same comparison after the pass is the one measurement that
can say the fix worked on real data rather than on fixtures.

**Which is why this is a capture/verify pair and not a single "is it right now?"
query.** AC 3 makes two claims, and only one of them is about the shifted rows:
*"the 198 Apple and 93 Prime shifted rows now agree, and the 104 + 35
already-correct rows are untouched."* A query run after the fact can see the
first. Nothing but a baseline can see the second, because a row that agrees today
looks identical whether it always did or whether it was broken and repaired — and
an over-correction that moved a correct row a day the *other* way is exactly the
failure a per-network offset would have produced. So `capture` records a verdict
per archive row, and `verify` diffs them one by one.

**The exit code inverts `jobs/reconcile.py`'s rule deliberately.** That harness
fails on any difference, gain or loss, because during a cutover window nothing
should move at all. Here movement is the point: a row going from *a day early*
to *agrees* is the ticket succeeding. What must never happen is a disagreement
getting **bigger** — so that, and only that, is the failure. `_is_regression`
is the whole of the rule and it is stated once.

**The artifact travels on stdin and stdout**, for the reason `reconcile` does:
`docs/` is not in the production image and a Coolify container is replaced on
every deploy, so a file written inside one is unreachable and short-lived. That
is what lets this run over `ssh 'docker exec -i ...'` against prod.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app import models as am
from tvbf.catalog import models as m
from tvbf.sql_fold import folded

log = logging.getLogger(__name__)

# The three shows AC 2 names, and the default for `show_report`. Not a filter
# anything else depends on — `--show` overrides it — but they are the shows the
# operator reported and the ones the ticket closes against.
AC2_SHOWS: tuple[str, ...] = ("Silo", "Lucky", "Ted Lasso")

# What a row's verdict can be. `one_day_late` is split out from `other` rather
# than folded into it because it is the signature of an *over*-correction —
# the failure mode a per-network or weekday rule would have produced, and the
# one a bare "does it agree now?" check cannot see.
AGREES = "agrees"
ONE_DAY_EARLY = "one_day_early"
ONE_DAY_LATE = "one_day_late"
OTHER = "other"
UNDATED = "undated"
UNRESOLVED = "unresolved"

# Buckets that carry no comparable delta. A row moving into one of these has
# stopped being evidence, which `_is_regression` treats as a loss rather than
# as neutral — silently dropping out of the denominator is how a regression
# hides.
INCOMPARABLE = frozenset({UNDATED, UNRESOLVED})

_EPISODE_RECORD_TYPES = ("episode_watch", "episode_rating")

# Enough to see the current season and the tail of the one before it, which is
# what a schedule check actually needs; the whole of a long-running show would
# bury it.
_EPISODES_PER_SHOW = 30


def bucket_for(delta_days: int | None) -> str:
    """The verdict for one row, from `catalog.air_date - watch_archive.episode_airdate`.

    Negative is the bug: TMDB recording the Pacific day puts our date *before*
    the one TV Maze snapshotted, which is what a US Eastern viewer saw.
    """
    if delta_days is None:
        return UNDATED
    if delta_days == 0:
        return AGREES
    if delta_days == -1:
        return ONE_DAY_EARLY
    if delta_days == 1:
        return ONE_DAY_LATE
    return OTHER


@dataclass(frozen=True)
class ArchiveRow:
    """One archived episode watch or rating, resolved against the live catalog."""

    archive_id: int
    show_name: str
    season_number: int | None
    episode_number: int | None
    archived_airdate: date
    catalog_airdate: date | None
    catalog_tmdb_airdate: date | None
    networks: tuple[str, ...]
    resolved: bool

    @property
    def delta_days(self) -> int | None:
        if not self.resolved or self.catalog_airdate is None:
            return None
        return (self.catalog_airdate - self.archived_airdate).days

    @property
    def bucket(self) -> str:
        if not self.resolved:
            return UNRESOLVED
        return bucket_for(self.delta_days)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "delta_days": self.delta_days,
            "show": self.show_name,
            "season": self.season_number,
            "episode": self.episode_number,
            "archived_airdate": self.archived_airdate.isoformat(),
            "catalog_airdate": (
                None if self.catalog_airdate is None else self.catalog_airdate.isoformat()
            ),
            "networks": list(self.networks),
        }


async def _networks_by_show(session: AsyncSession, show_ids: Sequence[int]) -> dict[int, list[str]]:
    """`{show_id: [network name]}`. A show genuinely can carry several.

    TMDB returns `networks[]` and `catalog.show_network` is a join table for
    that reason, so the per-network table below counts a two-network show under
    both — which is why the totals are computed over rows and not by summing the
    network columns.
    """
    if not show_ids:
        return {}
    rows = (
        await session.execute(
            select(m.ShowNetwork.show_id, m.Network.name)
            .join(m.Network, m.Network.id == m.ShowNetwork.network_id)
            .where(m.ShowNetwork.show_id.in_(show_ids))
            .order_by(m.ShowNetwork.show_id, m.Network.name)
        )
    ).all()
    by_show: dict[int, list[str]] = {}
    for show_id, name in rows:
        by_show.setdefault(show_id, []).append(name)
    return by_show


async def load_archive_rows(session: AsyncSession) -> list[ArchiveRow]:
    """Every archived episode row, paired with the catalog episode it describes.

    **Resolution is by `(source_show_id, season_number, episode_number)`, not by
    `source_episode_id`.** The archive's episode ids are a pre-cutover snapshot,
    and two later passes moved out from under them: NEU-1126 re-pointed user
    history onto ingested twins and deleted the copies, and NEU-1146 retired the
    orphans that were left. The *show* id survived both, because NEU-1042
    preserved TV Maze's ids as the catalog surrogates and nothing has deleted a
    show. So the season/episode pair is the durable key, and the id is used only
    to break a tie.

    A row that resolves to two episodes is left unresolved rather than
    arbitrated — the same rule every other pass in this repo applies to an
    ambiguous key, and here a wrong pairing would silently score the fix against
    an episode nobody watched.
    """
    archived = (
        await session.execute(
            select(
                am.WatchArchive.id,
                am.WatchArchive.show_name,
                am.WatchArchive.source_show_id,
                am.WatchArchive.source_episode_id,
                am.WatchArchive.season_number,
                am.WatchArchive.episode_number,
                am.WatchArchive.episode_airdate,
            )
            .where(
                am.WatchArchive.record_type.in_(_EPISODE_RECORD_TYPES),
                am.WatchArchive.episode_airdate.is_not(None),
            )
            .order_by(am.WatchArchive.id)
        )
    ).all()
    if not archived:
        return []

    show_ids = {r.source_show_id for r in archived}
    episodes = (
        await session.execute(
            select(
                m.Episode.id,
                m.Episode.show_id,
                m.Episode.season_number,
                m.Episode.episode_number,
                m.Episode.air_date,
                m.Episode.tmdb_air_date,
            ).where(m.Episode.show_id.in_(show_ids))
        )
    ).all()

    by_key: dict[tuple[int, int, int], list[Any]] = {}
    by_id: dict[int, Any] = {}
    for episode in episodes:
        by_id[episode.id] = episode
        by_key.setdefault(
            (episode.show_id, episode.season_number, episode.episode_number), []
        ).append(episode)

    networks = await _networks_by_show(session, sorted(show_ids))

    rows: list[ArchiveRow] = []
    for r in archived:
        match = None
        candidates = by_key.get((r.source_show_id, r.season_number, r.episode_number), [])
        if len(candidates) == 1:
            match = candidates[0]
        elif len(candidates) > 1:
            # A doubled key. Only the archived id can say which was meant, and
            # if it names none of them the row stays unresolved.
            match = next((c for c in candidates if c.id == r.source_episode_id), None)
        elif r.source_episode_id in by_id:
            match = by_id[r.source_episode_id]

        rows.append(
            ArchiveRow(
                archive_id=r.id,
                show_name=r.show_name,
                season_number=r.season_number,
                episode_number=r.episode_number,
                archived_airdate=r.episode_airdate,
                catalog_airdate=None if match is None else match.air_date,
                catalog_tmdb_airdate=None if match is None else match.tmdb_air_date,
                networks=tuple(networks.get(r.source_show_id, ())),
                resolved=match is not None,
            )
        )
    return rows


def _tally(rows: Iterable[ArchiveRow]) -> dict[str, int]:
    counts = {b: 0 for b in (AGREES, ONE_DAY_EARLY, ONE_DAY_LATE, OTHER, UNDATED, UNRESOLVED)}
    for row in rows:
        counts[row.bucket] += 1
    return counts


def build_snapshot(rows: Sequence[ArchiveRow]) -> dict[str, Any]:
    """The artifact. Byte-identical between two runs of an unchanged database.

    Keyed by archive row id rather than summarised, because the claim that has
    to survive is per row: *the already-correct rows are untouched*. Totals
    alone cannot distinguish a hundred repairs from a hundred repairs plus a
    hundred new breakages.
    """
    by_network: dict[str, dict[str, int]] = {}
    for network in sorted({n for row in rows for n in row.networks}):
        by_network[network] = _tally(r for r in rows if network in r.networks)
    unattributed = [r for r in rows if not r.networks]
    if unattributed:
        by_network["(no network)"] = _tally(unattributed)

    return {
        "ticket": "NEU-1145",
        "totals": _tally(rows),
        # Each archive row counts once in `totals` and once per network here, so
        # the columns deliberately do not sum to the total for a show carrying
        # more than one network.
        "by_network": by_network,
        "rows": {str(row.archive_id): row.to_dict() for row in rows},
    }


def _is_regression(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Did this row get worse? The whole of the failure rule, in three clauses.

    **The row stopped being comparable at all**, having been comparable before.
    An episode that no longer resolves has left the denominator, and a shrinking
    denominator is how a regression hides inside an improving percentage.

    **The disagreement grew.** This covers a correct row being moved at all,
    since `abs(0)` is smaller than everything.

    **The disagreement changed sign without reaching zero.** Not covered by the
    clause above, and the omission is not academic: a day early becoming a day
    late leaves `abs(delta)` identical while meaning we took a date that was one
    day wrong and made it one day wrong in the other direction. That is the
    over-correction signature — precisely what a per-network or weekday rule
    would have produced on the 17 Prime Video rows §2.6 measured — so it must
    fail rather than be filed as a movement.
    """
    if before["bucket"] in INCOMPARABLE:
        return False
    if after["bucket"] in INCOMPARABLE:
        return True
    old_delta, new_delta = before["delta_days"], after["delta_days"]
    if abs(new_delta) > abs(old_delta):
        return True
    return new_delta != 0 and (new_delta > 0) != (old_delta > 0)


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Diff two snapshots row by row.

    Rows the baseline does not know about are reported as `added` rather than
    judged: `app.watch_archive` is append-only and keeps growing as people watch
    things, so a new row is the app working, not a change to be scored.
    """
    old_rows = baseline.get("rows", {})
    new_rows = current.get("rows", {})

    corrected: list[dict[str, Any]] = []
    regressed: list[dict[str, Any]] = []
    other_moves: list[dict[str, Any]] = []
    for key, after in new_rows.items():
        before = old_rows.get(key)
        if before is None:
            continue
        if before["bucket"] == after["bucket"]:
            continue
        move = {
            "archive_id": int(key),
            "show": after["show"],
            "season": after["season"],
            "episode": after["episode"],
            "from": before["bucket"],
            "to": after["bucket"],
            "archived_airdate": after["archived_airdate"],
            "catalog_airdate": after["catalog_airdate"],
        }
        if _is_regression(before, after):
            regressed.append(move)
        elif after["bucket"] == AGREES:
            corrected.append(move)
        else:
            other_moves.append(move)

    return {
        "corrected": corrected,
        "regressed": regressed,
        "other_movements": other_moves,
        "added": sorted(set(new_rows) - set(old_rows), key=int),
        "still_early": [
            {"archive_id": int(k), "show": v["show"], "season": v["season"]}
            for k, v in sorted(new_rows.items(), key=lambda kv: int(kv[0]))
            if v["bucket"] == ONE_DAY_EARLY
        ],
        "baseline_totals": baseline.get("totals", {}),
        "current_totals": current.get("totals", {}),
    }


async def show_report(
    session: AsyncSession, *, show_names: Sequence[str] = AC2_SHOWS
) -> dict[str, Any]:
    """What AC 2 needs a human to read, per named show and season.

    There is no machine-readable Apple schedule to check against — the criterion
    is *"hand-verified against Apple's published schedule"* — so this cannot
    assert. What it can do is put the three values that matter side by side: the
    date we now serve, the raw TMDB value it was derived from, and the offset
    that separates them. A season whose offset is absent and whose dates still
    look a day early is the interesting row, and the refusal log says why.

    Names are matched through `sql_fold.folded`, the repo's one text fold, so
    "Ted Lasso" finds the show whatever punctuation or accents it carries.
    """
    report: dict[str, Any] = {"ticket": "NEU-1145", "shows": []}
    for name in show_names:
        shows = (
            await session.execute(
                select(m.Show.id, m.Show.name, m.Show.first_air_date, m.Show.tmdb_first_air_date)
                .where(folded(m.Show.name) == folded(name))
                .order_by(m.Show.id)
            )
        ).all()
        if not shows:
            report["shows"].append({"requested": name, "matched": []})
            continue

        matched = []
        for show in shows:
            offsets = {
                r.season_number: r.offset_days
                for r in (
                    await session.execute(
                        select(m.AirDateOffset.season_number, m.AirDateOffset.offset_days).where(
                            m.AirDateOffset.show_id == show.id
                        )
                    )
                ).all()
            }
            episodes = (
                await session.execute(
                    select(
                        m.Episode.season_number,
                        m.Episode.episode_number,
                        m.Episode.name,
                        m.Episode.air_date,
                        m.Episode.tmdb_air_date,
                    )
                    .where(m.Episode.show_id == show.id, m.Episode.air_date.is_not(None))
                    .order_by(m.Episode.season_number.desc(), m.Episode.episode_number.desc())
                    .limit(_EPISODES_PER_SHOW)
                )
            ).all()
            matched.append(
                {
                    "show_id": show.id,
                    "name": show.name,
                    "first_air_date": (
                        None if show.first_air_date is None else show.first_air_date.isoformat()
                    ),
                    "tmdb_first_air_date": (
                        None
                        if show.tmdb_first_air_date is None
                        else show.tmdb_first_air_date.isoformat()
                    ),
                    "offsets": {
                        ("show_wide" if k is None else str(k)): v
                        for k, v in sorted(
                            offsets.items(), key=lambda kv: (kv[0] is not None, kv[0])
                        )
                    },
                    "recent_episodes": [
                        {
                            "season": e.season_number,
                            "episode": e.episode_number,
                            "name": e.name,
                            # What a viewer now sees.
                            "air_date": e.air_date.isoformat(),
                            # What TMDB sent, present only where we corrected it.
                            "tmdb_air_date": (
                                None if e.tmdb_air_date is None else e.tmdb_air_date.isoformat()
                            ),
                            "offset_applied": offsets.get(e.season_number, offsets.get(None, 0)),
                        }
                        for e in episodes
                    ],
                }
            )
        report["shows"].append({"requested": name, "matched": matched})
    return report


async def count_archive_rows(session: AsyncSession) -> int:
    """Total archived episode rows carrying a date — §2.4's 440."""
    return (
        await session.execute(
            select(func.count())
            .select_from(am.WatchArchive)
            .where(
                am.WatchArchive.record_type.in_(_EPISODE_RECORD_TYPES),
                am.WatchArchive.episode_airdate.is_not(None),
            )
        )
    ).scalar_one()
