"""Per-user, per-show reconciliation: did anybody lose anything? (NEU-1030)

The TMDB migration's acceptance test, not a report. The cutover ships only if a
run against the new spine matches the baseline captured against the old one
*exactly* — the same tracked shows, episode watches, ratings and activity events,
broken down to the (user, show) pair so a discrepancy names whose data moved and
which show it belonged to.

Three things shape the design:

* **Counts keyed by `(user_id, show_id)`, comparable across spines.** The
  migration preserves TV Maze ids as `catalog.show.id`, so a show's key is the
  same number before and after — which is the only reason a stored baseline can
  be diffed against a post-cutover run at all. Nothing here needs a mapping
  table.
* **The artifact is deterministic JSON.** Sorted users, sorted shows, sorted
  keys, trailing newline. Two runs of an unchanged database produce
  byte-identical output, so `git diff` and `diff` both work on it, and a
  baseline can live in the repo.
* **Any difference fails.** Losses and gains alike, reported with direction. A
  harness that warns is a harness that gets ignored during a migration window.

The artifact holds user *ids*, never emails: it is meant to be committed.
Human-readable names are resolved from the live database when a discrepancy is
reported, which is the only place they are needed.
"""

import logging
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

ARTIFACT_VERSION = 1

# Which catalog schema supplies `episode.show_id`. The value is interpolated into
# SQL, so — exactly as with `rate_budget.BUCKETS` — it may only ever come from
# this module-level registry, never from a caller's string.
# One entry since NEU-1051 dropped `tvmaze`. It stays a registry rather than
# collapsing to a constant for the reason `rate_budget.BUCKETS` stays one with a
# single source registered: the guard is that the value cannot come from a
# caller, and that property is worth keeping whatever the registry's size.
SPINES = {
    "catalog": "catalog",
}

DEFAULT_SPINE = "catalog"

# Every metric is per (user, show). `tracked_shows` is 0 or 1 per show and is
# counted the same way as the rest so one comparison loop covers all five.
METRICS = (
    "tracked_shows",
    "episode_watches",
    "show_ratings",
    "episode_ratings",
    "activity_events",
)


class UnknownSpine(ValueError):
    """A spine name outside the registry. Never let it reach the SQL."""


def _episode_schema(spine: str) -> str:
    if spine not in SPINES:
        raise UnknownSpine(f"unknown spine {spine!r}; expected one of {sorted(SPINES)}")
    return SPINES[spine]


def _queries(spine: str) -> dict[str, str]:
    """One `(user_id, show_id, count)` query per metric.

    Episode-grain metrics reach the show through `{schema}.episode`, which is
    what makes the spine a parameter at all. The joins are LEFT joins on purpose:
    an episode watch whose episode has vanished should surface as a row under a
    null show rather than disappear from the count — that is precisely the kind
    of loss this harness exists to catch.
    """
    episode = f"{_episode_schema(spine)}.episode"
    return {
        "tracked_shows": """
            SELECT user_id, show_id, count(*)
            FROM app.user_show_watch
            GROUP BY 1, 2
        """,
        "episode_watches": f"""
            SELECT w.user_id, e.show_id, count(*)
            FROM app.user_episode_watch w
            LEFT JOIN {episode} e ON e.id = w.episode_id
            GROUP BY 1, 2
        """,
        "show_ratings": """
            SELECT user_id, show_id, count(*)
            FROM app.user_show_rating
            GROUP BY 1, 2
        """,
        "episode_ratings": f"""
            SELECT r.user_id, e.show_id, count(*)
            FROM app.user_episode_rating r
            LEFT JOIN {episode} e ON e.id = r.episode_id
            GROUP BY 1, 2
        """,
        # `activity_event` is polymorphic with no foreign key, so the show is
        # resolved per target type: a show event points at the show directly, an
        # episode event through the episode. Anything else buckets to null.
        "activity_events": f"""
            SELECT a.actor_id,
                   CASE
                       WHEN a.target_type = 'show' THEN a.target_id
                       WHEN a.target_type = 'episode' THEN e.show_id
                   END,
                   count(*)
            FROM app.activity_event a
            LEFT JOIN {episode} e
                   ON a.target_type = 'episode' AND e.id = a.target_id
            GROUP BY 1, 2
        """,
    }


def _show_sort_key(show_id: int | None) -> tuple[int, int]:
    """Ascending show id with the null bucket last, deterministically."""
    return (1, 0) if show_id is None else (0, show_id)


async def build_snapshot(db: AsyncSession, *, spine: str = DEFAULT_SPINE) -> dict[str, Any]:
    """Count every tracked show, watch, rating and activity event by user and show."""
    counts: dict[str, dict[tuple[str, int | None], int]] = {}
    for metric, sql in _queries(spine).items():
        rows = (await db.execute(text(sql))).all()
        counts[metric] = {
            (str(user_id), None if show_id is None else int(show_id)): int(n)
            for user_id, show_id, n in rows
        }

    # Every user, including any with nothing recorded — a user who lost their
    # last row would otherwise vanish from both snapshots and diff clean.
    user_ids = {str(u) for u in (await db.execute(text('SELECT id FROM app."user"'))).scalars()}
    user_ids.update(user for metric in counts.values() for user, _ in metric)

    users = []
    for user_id in sorted(user_ids):
        show_ids = {show for metric in counts.values() for user, show in metric if user == user_id}
        shows = [
            {
                "show_id": show_id,
                **{m: counts[m].get((user_id, show_id), 0) for m in METRICS},
            }
            for show_id in sorted(show_ids, key=_show_sort_key)
        ]
        users.append(
            {
                "user_id": user_id,
                "totals": {m: sum(s[m] for s in shows) for m in METRICS},
                "shows": shows,
            }
        )

    return {
        "artifact_version": ARTIFACT_VERSION,
        "spine": spine,
        "totals": {
            "users": len(users),
            **{m: sum(u["totals"][m] for u in users) for m in METRICS},
        },
        "users": users,
    }


def _flatten(snapshot: dict[str, Any]) -> dict[tuple[str, int | None, str], int]:
    """`(user, show, metric) -> count`, so a diff needs no nested walking.

    Absent keys read as 0 on either side, which is what makes a removed user, a
    removed show and a removed row all compare the same way.
    """
    return {
        (user["user_id"], show["show_id"], metric): show[metric]
        for user in snapshot["users"]
        for show in user["shows"]
        for metric in METRICS
    }


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """Every difference between two snapshots, most-lost first.

    `delta` is negative for a loss and positive for a gain. Both fail the run —
    an unexpected gain during a cutover window means something ran that should
    not have — but the direction is what tells you which problem you have.
    """
    before, after = _flatten(baseline), _flatten(current)

    discrepancies = []
    for key in sorted(
        before.keys() | after.keys(),
        key=lambda k: (k[0], _show_sort_key(k[1]), k[2]),
    ):
        user_id, show_id, metric = key
        was, now = before.get(key, 0), after.get(key, 0)
        if was != now:
            discrepancies.append(
                {
                    "user_id": user_id,
                    "show_id": show_id,
                    "metric": metric,
                    "baseline": was,
                    "current": now,
                    "delta": now - was,
                }
            )

    # Accounts are compared separately, because a user holding no rows at all
    # contributes no key above — `build_snapshot` unions every `app."user"` row
    # in precisely so such a user is visible, and walking only `shows` here would
    # throw that away and let a vanished empty account diff clean.
    was_users = {u["user_id"] for u in baseline["users"]}
    now_users = {u["user_id"] for u in current["users"]}
    for user_id in sorted(was_users ^ now_users):
        present = int(user_id in now_users)
        discrepancies.append(
            {
                "user_id": user_id,
                "show_id": None,
                "metric": "user_accounts",
                "baseline": 1 - present,
                "current": present,
                "delta": present * 2 - 1,
            }
        )

    discrepancies.sort(key=lambda d: (d["delta"], d["user_id"], _show_sort_key(d["show_id"])))
    return discrepancies


async def describe(
    db: AsyncSession, discrepancies: Iterable[dict[str, Any]], *, spine: str = DEFAULT_SPINE
) -> list[str]:
    """One human-readable line per discrepancy, naming the user and the show.

    Names are looked up now rather than stored in the artifact, which keeps user
    emails out of a file meant to be committed. A name that cannot be resolved
    (a deleted user, a show the spine no longer has) still reports its id — the
    line has to survive exactly the failure it is describing.
    """
    discrepancies = list(discrepancies)
    if not discrepancies:
        return []

    user_ids = {UUID(d["user_id"]) for d in discrepancies}
    show_ids = {d["show_id"] for d in discrepancies if d["show_id"] is not None}

    # Expanding `IN` rather than `= ANY(:ids)`: asyncpg would have to infer an
    # array type for the parameter, which it cannot do for a bare uuid list.
    email_rows = (
        await db.execute(
            text('SELECT id, email FROM app."user" WHERE id IN :ids').bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": sorted(user_ids)},
        )
    ).all()
    emails: dict[UUID, str] = {row[0]: row[1] for row in email_rows}

    names: dict[int, str] = {}
    if show_ids:
        name_rows = (
            await db.execute(
                text(
                    f"SELECT id, name FROM {_episode_schema(spine)}.show WHERE id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": sorted(show_ids)},
            )
        ).all()
        names = {row[0]: row[1] for row in name_rows}

    lines = []
    for d in discrepancies:
        who = emails.get(UUID(d["user_id"]), f"<deleted user {d['user_id']}>")
        show_id = d["show_id"]
        if show_id is None:
            what = (
                "<no show — the account itself>"
                if d["metric"] == "user_accounts"
                else "<no show — the catalog row this pointed at is missing>"
            )
        else:
            what = f"{names.get(show_id, '<unknown show>')} (id {show_id})"
        verb = "LOST" if d["delta"] < 0 else "GAINED"
        lines.append(
            f"{verb} {abs(d['delta'])} {d['metric']} — {who} — {what} "
            f"(baseline {d['baseline']}, current {d['current']})"
        )
    return lines
