"""The taste payload: one user's history as the model's entire input.

Project spec §5.3 (shape), §5.4 (the generation floor) and §9.1 (the
regeneration hash). One object does **three jobs** — it is what the model is
told, it is what the regeneration gate hashes, and it is the exclusion list —
and building it once is what keeps the three from disagreeing.

## Columnar, because the label and the field names are paid for once

`{"columns": [...], "liked": [[...]], ...}` costs ~9 tokens a row against ~38 for
repeated-key objects carrying a per-row label: 4.7k tokens versus 20k on the
heaviest real account (522 shows). The header declares the schema once rather
than 522 times, and grouping by tier makes the label free rather than ~3 tokens
per row.

The three groups are always present, empty or not. A shape that varies with the
data is one the model has to infer, and one the hash would churn on.

**No `tmdb_id`, and the year rather than the date.** A recommendation is by
definition a show *not* in the input, so the model can never echo an id back —
and we built the payload, so we already know every id. It would be ~2k tokens
per call for a field with no consumer on either side. The premiere year
disambiguates a title; the month and day are three tokens no reasoning step
touches.

## Row order is part of the hash, so it is total and it is not the query's

Rows sort by folded title, then year, then show id. Without an explicit order a
query-plan change reorders rows, every hash differs, and every user regenerates
for nothing — the exact churn §9.1 exists to prevent. The show id is the last
term because the first two are not unique: two shows can share a folded title
and a premiere year, and "sorted" has to mean one arrangement rather than
whichever the planner happened to emit.

The fold is `sql_fold`'s and comes back from Postgres with the titles
(`show_repo.titles_for_ids`). It is not reproducible in Python — see that module
— so sorting on a Python-side fold would order the payload by a rule nothing else
in this codebase uses.

## The hash covers the prompt version and the model id, not just the bytes

`sha256(prompt_version \\n model \\n canonical_json)`. Both non-payload parts are
*in* the hash because otherwise editing the prompt changes what the pass would
produce while every hash still matches, and nobody ever regenerates; with them,
shipping a prompt change re-runs everyone exactly once, which is also the only
way to evaluate it against real users.

They are newline-separated rather than concatenated so that the parts cannot run
together: bare concatenation makes `("1", "a/b")` and `("1a", "/b")` the same
hash input, which is a silent skip rather than a loud one.

## Exclusion is a wider set than the rows

`excluded_show_ids` is every show the user has any record for — My Shows, any
episode watch, any show rating, **any episode rating** — while the rows are only
the shows that reached a tier, minus whatever the INTERESTED cap dropped. The two
deliberately differ, in the safe direction: a show the payload never mentions is
still one we must never recommend back.

That is why this module asks `episode_rating_repo` for its own list rather than
reading the tiers. `taste_for_user` deliberately does not let an episode rating
*enrol* a show — an episode rating refines a show already in the universe and is
not a signal of its own — but "we have no opinion about this show" and "the user
has never seen this show" are different claims, and only the second licenses a
recommendation.

## The floor is one expression with two named constants

`(2 x liked) + interested >= 10` reproduces both intuitive endpoints exactly — 5
LIKED alone qualifies, 10 INTERESTED alone qualifies — and covers the mixed
middle that two independent thresholds would refuse (3 LIKED + 4 INTERESTED
qualifies, as it should). The 2:1 weighting states the belief plainly: a show you
watched tells us about twice what a show you bookmarked does.

**NOT LIKED contributes nothing.** It is exclusion signal; you cannot generate
from a list of things somebody disliked.

The counts are the rows actually emitted, so the floor is a claim about the
payload rather than about the query behind it. The INTERESTED cap cannot change
the verdict either way — it caps at 50 and the floor is 10.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.repos import episode_rating_repo, show_repo
from tvbf.recommendations.taste import TasteLabel, TasteSignal, taste_for_user

PROMPT_VERSION = "3"
"""The version of the prompt this payload is hashed against (§9.1).

It lives here because the hash needs one and the hash is this module's. **Bump it
in the same commit as any change to the prompt text or to the payload's own
shape** — that is what makes a prompt change re-run every user exactly once
instead of never.
"""

COLUMNS = ("title", "year", "pct", "stars")
"""The header for the three taste groups, in the order every row is written."""

EXCLUDE_COLUMNS = ("title", "year")
"""The header for the `exclude` group, which carries no viewing data.

A second shape rather than padding these rows out to `COLUMNS`: there is nothing
to say about `pct` or `stars` for a show that reached no tier, and two nulls a row
across a long tail is real tokens spent asserting nothing.
"""

INTERESTED_CAP = 50
"""How many INTERESTED rows the payload carries, most recently added first.

Its signal is the weakest per item and it is the tier most likely to dominate the
payload by volume — 64% of My Shows rows have no watches at all.
"""

LIKED_WEIGHT = 2
"""What one LIKED show is worth against one INTERESTED show, for the floor."""

GENERATION_FLOOR = 10
"""The weighted total a user needs before anything is generated for them."""

_Row = list[str | int | float | None]
_ExcludeRow = list[str | int | None]


@dataclass(frozen=True, slots=True)
class TastePayload:
    """One user's compiled payload, its hash, and what the floor reads."""

    json: str
    hash: str
    liked_count: int
    interested_count: int
    interested_before_cap: int
    """How many shows reached INTERESTED before `INTERESTED_CAP` was applied.

    The rows alone cannot answer it: a payload carrying exactly 50 reads the same
    whether the user bookmarked 50 shows or 300. `--dry-run` (NEU-1105) is where
    the cap gets checked against a real account, and a rule whose effect is
    invisible is one nobody can check.
    """
    excluded_show_ids: frozenset[int]
    excluded_row_count: int
    """How many shows the `exclude` group names — those in no tier group.

    Reported rather than derived so `--dry-run` can say how much of the exclusion
    set the model can actually see. It is **not** `len(excluded_show_ids)`: the
    tier rows are excluded too, and they are visible as themselves.
    """

    @property
    def meets_floor(self) -> bool:
        """Whether there is enough here to generate from (§5.4)."""
        return LIKED_WEIGHT * self.liked_count + self.interested_count >= GENERATION_FLOOR


def to_canonical_json(
    rows: Mapping[TasteLabel, list[_Row]],
    excluded_rows: Sequence[_ExcludeRow] = (),
) -> str:
    """The payload's exact bytes: the headers, the three tier groups, then `exclude`.

    Every group is written whether or not it has rows, and the separators carry no
    incidental whitespace — the hash is over these bytes, so both are load-bearing
    rather than cosmetic. `ensure_ascii=False` keeps a title in its own script as
    itself instead of spending four tokens an escape on it.
    """
    return json.dumps(
        {
            "columns": list(COLUMNS),
            "exclude_columns": list(EXCLUDE_COLUMNS),
            TasteLabel.LIKED.value: rows.get(TasteLabel.LIKED, []),
            TasteLabel.NOT_LIKED.value: rows.get(TasteLabel.NOT_LIKED, []),
            TasteLabel.INTERESTED.value: rows.get(TasteLabel.INTERESTED, []),
            "exclude": list(excluded_rows),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def payload_hash(*, prompt_version: str, model: str, canonical_json: str) -> str:
    """The regeneration gate's key. See the module docstring for the separator."""
    return hashlib.sha256(
        "\n".join((prompt_version, model, canonical_json)).encode("utf-8")
    ).hexdigest()


async def build_payload(
    db: AsyncSession,
    *,
    user_id: UUID,
    model: str,
    prompt_version: str = PROMPT_VERSION,
    now: datetime | None = None,
) -> TastePayload:
    """Compile, serialize and hash one user's taste payload.

    `model` is required rather than read from settings for the reason
    `OpenAICompatClient` takes it: it is in the hash, so a payload built against
    one model id and compared against a set written under another must be a
    caller's mistake and not a default quietly agreeing with itself.
    """
    now_dt = now if now is not None else datetime.now(UTC)

    signals = await taste_for_user(db, user_id=user_id, now=now_dt)
    episode_rated_show_ids = await episode_rating_repo.mean_stars_per_show_for_user(
        db, user_id=user_id
    )
    excluded = frozenset(signals) | frozenset(episode_rated_show_ids)

    grouped, interested_before_cap = _group(signals)
    shown_ids = {sid for ids in grouped.values() for sid in ids}
    # Titles for the whole exclusion set, not just the tiers: the `exclude` group
    # needs a title for every show the model must not name, and one query over
    # the union is cheaper than two.
    titles = await show_repo.titles_for_ids(db, sorted(shown_ids | excluded))
    rows = {label: _rows(ids, signals, titles) for label, ids in grouped.items()}
    excluded_rows = _exclude_rows(excluded - shown_ids, titles)

    canonical_json = to_canonical_json(rows, excluded_rows)

    return TastePayload(
        json=canonical_json,
        hash=payload_hash(
            prompt_version=prompt_version, model=model, canonical_json=canonical_json
        ),
        liked_count=len(rows[TasteLabel.LIKED]),
        interested_count=len(rows[TasteLabel.INTERESTED]),
        interested_before_cap=interested_before_cap,
        excluded_show_ids=excluded,
        excluded_row_count=len(excluded_rows),
    )


def _group(signals: dict[int, TasteSignal]) -> tuple[dict[TasteLabel, list[int]], int]:
    """Show ids per tier, with INTERESTED capped to the most recently added.

    The cap selects by recency and the rows are then ordered by title, so the two
    are separate questions: `added_at` decides *which* 50, `_rows` decides how
    they read. The show id breaks an `added_at` tie so the selection is total —
    two shows added in the same transaction share a timestamp.

    The second element is how many reached INTERESTED before the cap, counted
    here rather than re-derived by a caller so that the cap and the number
    describing it cannot drift apart.
    """
    grouped: dict[TasteLabel, list[int]] = {label: [] for label in TasteLabel}
    for show_id, signal in signals.items():
        if signal.label is not None:
            grouped[signal.label].append(show_id)

    interested = grouped[TasteLabel.INTERESTED]
    interested.sort(key=lambda sid: (_added_at_key(signals[sid]), sid), reverse=True)
    grouped[TasteLabel.INTERESTED] = interested[:INTERESTED_CAP]
    return grouped, len(interested)


def _added_at_key(signal: TasteSignal) -> datetime:
    """`added_at` for the cap's sort, treating a missing one as the oldest.

    Every INTERESTED show is in My Shows and `user_show_watch.created_at` is NOT
    NULL, so this is unreachable today. It exists because the alternative to a
    floor is a `TypeError` in a weekly job if that ever stops being true.
    """
    return signal.added_at if signal.added_at is not None else datetime.min.replace(tzinfo=UTC)


def _rows(
    show_ids: list[int],
    signals: dict[int, TasteSignal],
    titles: dict[int, show_repo.ShowTitle],
) -> list[_Row]:
    """One tier's rows, in the payload's total order.

    A show with no `catalog.show` row is dropped: there is no title to report, and
    it stays in `excluded_show_ids` regardless, which is the half that matters.
    """
    present = [sid for sid in show_ids if sid in titles]
    present.sort(key=lambda sid: (titles[sid].folded_name, _year_key(titles[sid].year), sid))
    return [
        [
            titles[sid].name,
            titles[sid].year,
            signals[sid].completion.pct,
            _stars(signals[sid].stars),
        ]
        for sid in present
    ]


def _exclude_rows(
    show_ids: AbstractSet[int],
    titles: dict[int, show_repo.ShowTitle],
) -> list[_ExcludeRow]:
    """The `exclude` group: every show the model must not name that no tier shows.

    These are the shows §8's filter drops that the payload otherwise never
    mentions — the INTERESTED cap's overflow, a show carrying only an episode
    rating, a show no tier rule covers. Before this group existed the model could
    not avoid them, because it was never told they were there, and every one it
    named was silently discarded after the call. A 2026-08-17 production run named
    25 of 25 titles that were already in its input and stored none of them.

    Sorted on `_rows`' key so the order is total: the hash is over these bytes,
    and a query-plan change reordering them would regenerate every user for
    nothing.

    A show with no `catalog.show` row is dropped, on `_rows`' reasoning — there is
    no title to name it with, and it stays in `excluded_show_ids` regardless,
    which is the half that matters.
    """
    present = [sid for sid in show_ids if sid in titles]
    present.sort(key=lambda sid: (titles[sid].folded_name, _year_key(titles[sid].year), sid))
    return [[titles[sid].name, titles[sid].year] for sid in present]


def _year_key(year: int | None) -> int:
    """Undated shows sort ahead of dated ones, deterministically."""
    return year if year is not None else -1


def _stars(stars: float | None) -> float | None:
    """The rating as the payload reports it: one decimal place, or nothing.

    A show rating is already a half star. A rating synthesized from the mean of
    episode ratings is not, and `3.3333333333333335` is a dozen tokens claiming a
    precision the underlying half-stars do not have. **The rounding is the
    payload's alone** — `taste.py` classifies on the raw mean, on purpose, because
    rounding there would move a value across a tier boundary.
    """
    return None if stars is None else round(stars, 1)
