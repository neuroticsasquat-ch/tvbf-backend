"""The NEU-1163 §5 backfill derivation, run as SQL exactly as it ships.

The expressions under test are imported from the migration module rather than
restated here — a test carrying its own copy of a `regexp_replace` chain asserts
against itself. `derive_handles` takes a source relation, so `upgrade` points it
at `app."user"` and this points it at a `VALUES` list, and both run the same
text.

The migration is not applied to the test database (the suite builds `app` with
`create_all`), which is exactly why this exercises the derivation as a query
instead of running `alembic upgrade`.
"""

import importlib.util
import pathlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "d5a91c4e2f68_add_user_handle.py"
)


def _load_migration():
    """Import the revision by path. `migrations/versions` is not a package and
    is not on `sys.path`, so a plain import cannot reach it."""
    spec = importlib.util.spec_from_file_location("_neu1163_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()


async def _derive(session, rows: list[tuple[uuid.UUID, str, datetime]]) -> dict[uuid.UUID, str]:
    values = ", ".join(
        f"('{rid}'::uuid, :name{i}, '{created.isoformat()}'::timestamptz)"
        for i, (rid, _, created) in enumerate(rows)
    )
    params = {f"name{i}": name for i, (_, name, _) in enumerate(rows)}
    sql = migration.derive_handles(
        f"SELECT * FROM (VALUES {values}) AS v(id, display_name, created_at)"
    )
    result = await session.execute(text(sql), params)
    return {row.id: row.handle for row in result}


def _row(name: str, *, offset_minutes: int = 0) -> tuple[uuid.UUID, str, datetime]:
    return (
        uuid.uuid4(),
        name,
        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=offset_minutes),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("display_name", "expected"),
    [
        ("Tom Boone", "tom_boone"),
        ("jeanne_briggs", "jeanne_briggs"),
        ("Renée O'Hara", "renee_o_hara"),
        # `immutable_unaccent` is the part Python cannot reproduce:
        # `unicodedata` does not decompose the stroked L, so a Python-side fold
        # would yield `ukasz` here.
        ("Łukasz", "lukasz"),
        # Leading non-letters are trimmed rather than the value being refused.
        ("99 Problems", "problems"),
    ],
)
async def test_the_tabulated_derivations(session, display_name, expected):
    rows = [_row(display_name)]
    derived = await _derive(session, rows)
    assert derived[rows[0][0]] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("display_name", ["Jo", "Тимофей", "🎬", "", "   ", "---"])
async def test_a_display_name_that_yields_nothing_usable_falls_back(session, display_name):
    """§5.2. `user_<8 hex>` is the one fallback for every failure, and **no
    input can fail** — a name that folds to the empty string, one below the
    3-character floor, and one in a script the charset strip removes all land
    here rather than raising."""
    rows = [_row(display_name)]
    row_id = rows[0][0]
    derived = await _derive(session, rows)
    assert derived[row_id] == f"user_{str(row_id)[:8]}"


@pytest.mark.asyncio
async def test_a_reserved_word_falls_back_rather_than_taking_a_suffix(session):
    """§5.2, superseding the ticket's AC 3. `admin2` would be precisely the
    impersonation the blocklist exists to stop — a numeric suffix does not
    weaken a name, it decorates it."""
    rows = [_row("Admin")]
    row_id = rows[0][0]
    derived = await _derive(session, rows)
    assert derived[row_id] == f"user_{str(row_id)[:8]}"


@pytest.mark.asyncio
async def test_the_oldest_account_keeps_the_bare_stem_on_a_collision(session):
    """§5.2. Ordered by `(created_at, id)` — the ordering `user_repo.list_ids`
    already states, for the same reason: an ordering that is written down is one
    a test can assert and a re-run can reproduce."""
    older = _row("Tom Boone", offset_minutes=0)
    newer = _row("Tom  Boone", offset_minutes=10)
    third = _row("tom boone", offset_minutes=20)
    derived = await _derive(session, [newer, third, older])
    assert derived[older[0]] == "tom_boone"
    assert derived[newer[0]] == "tom_boone2"
    assert derived[third[0]] == "tom_boone3"


@pytest.mark.asyncio
async def test_a_suffixed_collision_stays_inside_the_ceiling(session):
    """`left(stem, 30 - length(rn::text))` is what keeps the suffix from
    pushing the value past 30 characters and out of the shape its own validator
    would accept."""
    name = "Abcdefghij Klmnopqrst Uvwxyzabcd Efgh"
    rows = [_row(name, offset_minutes=i) for i in range(2)]
    derived = await _derive(session, rows)
    handles = set(derived.values())
    assert len(handles) == 2
    for handle in handles:
        assert len(handle) <= 30
    # The older row keeps the bare (truncated) stem; the newer takes the suffix,
    # and `left(...)` is what makes room for it inside the ceiling.
    assert derived[rows[0][0]] == "abcdefghij_klmnopqrst_uvwxyzab"
    assert derived[rows[1][0]] == "abcdefghij_klmnopqrst_uvwxyza2"


@pytest.mark.asyncio
async def test_a_name_deriving_to_the_anonymisation_shape_takes_its_own_id(session):
    """Left alone, `user_3f4a2b1c` as a *display name* would hand this account
    an identifier keyed to some other account's id — the identity inheritance
    the pattern refusal in `schemas.Handle` exists to prevent."""
    rows = [_row("user_3f4a2b1c")]
    row_id = rows[0][0]
    derived = await _derive(session, rows)
    assert derived[row_id] == f"user_{str(row_id)[:8]}"


@pytest.mark.asyncio
async def test_every_derived_value_passes_the_validator(session):
    """The property §5.3's first assertion enforces at migration time: a
    derivation that produced an invalid handle would leave an account whose own
    validator refuses its identifier."""
    from tvbf.app.schemas import HandleUpdateRequest

    names = [
        "Tom Boone",
        "Renée O'Hara",
        "Łukasz",
        "99 Problems",
        "Jo",
        "Тимофей",
        "🎬",
        "Admin",
        "!!!",
        "___",
        "A" * 200,
    ]
    rows = [_row(n, offset_minutes=i) for i, n in enumerate(names)]
    derived = await _derive(session, rows)
    assert len(derived) == len(names)
    for row_id, handle in derived.items():
        if handle == f"user_{str(row_id)[:8]}":
            # The fallback is deliberately a shape the validator refuses by
            # pattern (§1.2) — nobody may *claim* it, and the backfill is the
            # only thing that may assign it.
            continue
        assert HandleUpdateRequest(handle=handle).handle == handle
