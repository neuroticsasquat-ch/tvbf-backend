"""The migration's inlined reserved list, pinned as it shipped (NEU-1163 §3.2).

`migrations/versions/d5a91c4e2f68_add_user_handle.py` restates
`app/handles.py:RESERVED_HANDLES` as a SQL array literal, because no migration
in this repo imports application code — a migration must keep meaning what it
meant on the day it ran, while `handles.py` is free to change.

**This is a snapshot test, not an equality test.** Asserting the two are equal
today would turn every later edit to `handles.py` — extending the list, or
un-reserving a word — into a failure demanding that a shipped migration be
rewritten, which is exactly the coupling the duplication exists to avoid. What
is pinned instead is the migration's own copy: it must still be the list that
shipped, and it must still be well-formed.
"""

import hashlib
import importlib.util
import pathlib
import re

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "d5a91c4e2f68_add_user_handle.py"
)

# sha256 over the sorted, newline-joined entries of the migration's array as of
# revision d5a91c4e2f68 — the list `app/handles.py` held on 2026-08-21, when
# both were written from one generator run.
_SNAPSHOT_SHA256 = "f69a6d0076ce0ded694d168fcc8dd318e81b6157c4f9f89bfbee0d575e6d46d1"
_SNAPSHOT_COUNT = 466


def _migration_reserved() -> list[str]:
    spec = importlib.util.spec_from_file_location("_neu1163_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return sorted(re.findall(r"'([a-z0-9_]+)'", module._RESERVED))


def test_the_migration_list_is_the_one_that_shipped():
    entries = _migration_reserved()
    assert len(entries) == _SNAPSHOT_COUNT
    digest = hashlib.sha256("\n".join(entries).encode()).hexdigest()
    assert digest == _SNAPSHOT_SHA256


def test_every_migration_entry_is_a_claimable_shape():
    """An entry the charset could never produce is unreachable, and an
    unreachable entry is how a blocklist stops meaning what it says."""
    shape = re.compile(r"^[a-z][a-z0-9_]{2,29}$")
    assert [e for e in _migration_reserved() if not shape.match(e)] == []


def test_the_two_copies_agreed_at_this_revision():
    """The one thing worth asserting *across* the seam, and the reason it is
    phrased as a subset: `handles.py` may grow freely, and the migration must
    not be edited to chase it. A word later *un-reserved* in the module is the
    one case that fails here, and it should — un-reserving something the
    migration already refused to hand out is a decision worth stopping on.
    """
    from tvbf.app.handles import RESERVED_HANDLES

    assert set(_migration_reserved()) <= RESERVED_HANDLES
