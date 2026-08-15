"""repoint the `app.*` (and `import_ne`) foreign keys onto `catalog` (NEU-1046)

**A constraint swap, not a data migration.** No row's values change. NEU-1042
copied `tvmaze.{show,episode}` into `catalog` with the ids preserved (ADR-0008),
so every `show_id` and `episode_id` `app` already holds resolves against
`catalog` unchanged — which is the whole reason the cutover is seconds of DDL
rather than a rewrite of user history.

Five constraints move:

| Table | Column | Was | Becomes |
| -- | -- | -- | -- |
| `app.user_show_watch` | `show_id` | `tvmaze.show.id` | `catalog.show.id` |
| `app.user_show_rating` | `show_id` | `tvmaze.show.id` | `catalog.show.id` |
| `app.user_episode_watch` | `episode_id` | `tvmaze.episode.id` | `catalog.episode.id` |
| `app.user_episode_rating` | `episode_id` | `tvmaze.episode.id` | `catalog.episode.id` |
| `import_ne.show_resolution` | `show_id` | `tvmaze.show.id` | `catalog.show.id` |

**`ON DELETE` behaviour is carried across verbatim**, and that is the single
most dangerous thing here to get wrong quietly. The four `app` constraints
cascade because a tombstoned show must never orphan a watch record; the
`import_ne` one is deliberately NO ACTION, which is what makes a delete of a
referenced show fail loudly rather than silently shed 522 resolution rows
(ADR-0005 leans on exactly that). Each `ondelete=` below is copied from
`pg_get_constraintdef` rather than inferred, and the acceptance check is that
the definitions differ in nothing but the referenced schema.

**`app.activity_event` is not in the table** — it is polymorphic and carries no
FK at all, so there is nothing to repoint. That is also why it gets no
referential protection and why the reconciliation harness counts its rows
explicitly instead.

**The migration asserts what it is entitled to assume.** `ALTER TABLE ... ADD
CONSTRAINT` would fail on an unresolvable id anyway, but it fails naming one
row, in the middle of a cutover window, with no indication of scale or remedy.
`_assert_resolvable` runs the anti-join first and reports the count per table,
so an operator sees "3 of 8,499 `app.user_episode_watch` rows have no
`catalog.episode`" — whose fix is `task copy:catalog`, not a rollback.

**`import_ne` is handled conditionally, and the probe asks about the constraint
rather than the table.** That schema is created by the one-off Next Episode
import, not by `task db:init` and not by any migration, so it is absent from CI,
from the test database and from a fresh developer machine — without the probe
this migration would run only on the two machines that ever ran that import. It
reads `pg_constraint` because a table can be there without its foreign key:
`CLAUDE.md` records the ratings and `import_ne` constraints having been dropped
silently by a refresh that replayed a hardcoded list, and on such a database a
table-existence probe would send a `DROP CONSTRAINT` at a constraint that is not
there, failing the `alembic upgrade head` the container runs before uvicorn.

**One thing this deliberately does not fix.** `app.user_show_watch.show_id` and
`app.user_episode_watch.episode_id` are `integer`, while `catalog.show.id` and
`catalog.episode.id` are `bigint` — so the repoint turns two same-width foreign
keys into cross-width ones, and a future catalog id above 2^31 could not be
tracked or marked watched. It is not urgent: those identities start at 1,000,000
and 10,000,000 against a catalog of ~229,000 shows, and the two *rating* tables
have always been the mismatch in the other direction. Widening them is an
`ALTER COLUMN TYPE` on user tables, which is a data migration and therefore not
this ticket's; it is called out here so the next reader does not have to
rediscover it.

Revision ID: b6d24f0ac715
Revises: f85a608ef19e
Create Date: 2026-08-12 00:00:00.000000+00:00

"""

from collections.abc import Sequence
from dataclasses import dataclass

from alembic import op
from sqlalchemy import text

revision: str = "b6d24f0ac715"
down_revision: str | Sequence[str] | None = "f85a608ef19e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The two spines a constraint can point at. Both are interpolated into SQL, so
# like `rate_budget`'s `Bucket.table` and `reconcile`'s `--spine` they may only
# ever come from this registry — the two callers below pass a literal today, and
# this is what keeps that true of the third.
_SPINES: frozenset[str] = frozenset({"catalog", "tvmaze"})


@dataclass(frozen=True)
class _Repoint:
    """One constraint's move, in both directions.

    `on_delete` is the behaviour the constraint already has and must keep;
    `optional` marks a table whose schema may not exist on this database.
    """

    schema: str
    table: str
    column: str
    constraint: str
    target_table: str
    on_delete: str | None
    optional: bool = False


_REPOINTS: tuple[_Repoint, ...] = (
    _Repoint("app", "user_show_watch", "show_id", "fk_usw_show", "show", "CASCADE"),
    _Repoint(
        "app", "user_show_rating", "show_id", "fk_user_show_rating_show", "show", "CASCADE"
    ),
    _Repoint("app", "user_episode_watch", "episode_id", "fk_uew_episode", "episode", "CASCADE"),
    _Repoint(
        "app",
        "user_episode_rating",
        "episode_id",
        "fk_user_episode_rating_episode",
        "episode",
        "CASCADE",
    ),
    _Repoint(
        "import_ne",
        "show_resolution",
        "show_id",
        "show_resolution_show_id_fkey",
        "show",
        None,
        optional=True,
    ),
)


def _applies_here(bind, repoint: _Repoint) -> bool:
    """Whether this constraint is actually on the database being migrated.

    It probes `pg_constraint` rather than the table, and the difference is the
    one that would take production down. A schema present without its foreign
    key is not hypothetical here — `CLAUDE.md` records the ratings and
    `import_ne` constraints having been dropped silently by an earlier refresh —
    and on such a database a table-existence probe would wave the row through to
    a `DROP CONSTRAINT` that raises. That lands on the `alembic upgrade head`
    the container's `CMD` runs before uvicorn, so the app would not come up.

    Absent means absent: the migration moves the constraint that exists and
    declines to invent one that does not.
    """
    if not repoint.optional:
        return True
    found = bind.execute(
        text(
            "SELECT 1 FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE c.contype = 'f' AND c.conname = :constraint "
            "AND n.nspname = :schema AND t.relname = :table"
        ),
        {
            "constraint": repoint.constraint,
            "schema": repoint.schema,
            "table": repoint.table,
        },
    ).scalar()
    return found is not None


def _assert_resolvable(bind, repoint: _Repoint, spine: str) -> None:
    """Fail before the DDL does, with the count and the remedy.

    NULLs are excluded because a foreign key ignores them, and counting them
    here would report a violation the constraint would have accepted.
    """
    orphans = bind.execute(
        text(
            f"SELECT count(*) FROM {repoint.schema}.{repoint.table} src "  # noqa: S608
            f"LEFT JOIN {spine}.{repoint.target_table} dst ON dst.id = src.{repoint.column} "
            f"WHERE src.{repoint.column} IS NOT NULL AND dst.id IS NULL"
        )
    ).scalar_one()
    if orphans:
        raise RuntimeError(
            f"{orphans} row(s) in {repoint.schema}.{repoint.table} have a "
            f"{repoint.column} with no {spine}.{repoint.target_table} row. "
            f"The id-preserving copy is incomplete — run `task copy:catalog` "
            f"and re-run this migration."
        )


def _move(spine: str) -> None:
    if spine not in _SPINES:
        raise ValueError(f"unknown spine {spine!r}; expected one of {sorted(_SPINES)}")
    bind = op.get_bind()
    for repoint in _REPOINTS:
        if not _applies_here(bind, repoint):
            continue
        _assert_resolvable(bind, repoint, spine)
        op.drop_constraint(
            repoint.constraint, repoint.table, schema=repoint.schema, type_="foreignkey"
        )
        op.create_foreign_key(
            repoint.constraint,
            repoint.table,
            repoint.target_table,
            [repoint.column],
            ["id"],
            source_schema=repoint.schema,
            referent_schema=spine,
            ondelete=repoint.on_delete,
        )


def upgrade() -> None:
    _move("catalog")


def downgrade() -> None:
    # Reversible only while `tvmaze` still stands. It does: dropping that schema
    # is NEU-1050's, deliberately a separate and later ticket, which is what
    # leaves the old mirror as a recovery path through the risk window.
    _move("tvmaze")
