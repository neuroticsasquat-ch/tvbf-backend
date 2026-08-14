"""Retire every TV Maze orphan row from the catalog spine (NEU-1146).

NEU-1042 copied TV Maze's catalog into `catalog` with `tmdb_id IS NULL`.
NEU-1126 re-pointed everything that paired on the exact
`(show, season, episode)` key and deleted the copies. What it could not reach is
this ticket: **782,161 episode rows, 18,341 season rows and 2 show rows** still
holding TV Maze titles, airdates and numbering, measured against production on
2026-08-14.

While those rows are served, CC BY-SA 4.0 attribution is a licence condition —
which is why `tvbf-frontend` still carries a TVmaze credit in its footer. This
pass empties the set so that credit can come out: every orphan with a TMDB
counterpart is re-pointed onto it, every orphan without one is deleted, at all
three grains.

**This reverses a standing decision.** ADR-0008 sanctioned `tmdb_id IS NULL` as
the way to hold content TMDB lacks, and the migration's stated constraint was
that no user loses a watched episode *even where TMDB has no counterpart*. Both
are superseded: sole-sourcing wins, and the ~95 watch records the report names
are the measured, accepted cost. ADR-0012 records the reversal. `report` mode
prints that loss list **before** anything is spent, and it is meant to be read.

## Why the exact key failed, and what replaces it

TV Maze and TMDB disagree about **what an episode is**, not about what aired.
Three genuinely different things are tangled in the residue, and the tiers below
exist to separate them:

* *Same episode, different address.* NEU-1042 numbered TV Maze's null-numbered
  specials **negative** within their original season; TMDB parks specials in
  season 0 with real numbers. Pure re-addressing.
* *Same episode, different metadata.* Lost's *Missing Pieces* carries a uniform
  seven-day date shift and punctuation the fold removes.
* *No such episode.* Friends `s4e24 "…Part II"` — TMDB counts that two-parter as
  **one** episode. Not missing content; the same content counted differently.

## The four tiers, in order, first hit wins

**Tier 0 — exact key, same show.** `(show_id, season_number, episode_number)`
with exactly one ingested and one orphan row. NEU-1126's rule, unchanged. It is
not spent: 880 rows had accrued from catalog deltas by 2026-08-14, the same
"re-run after any later ingest or delta" property `season_dedupe` carries.

**Tier 1 — unique folded title, same show.** The title belongs to exactly one
orphan and exactly one ingested episode within the show. **The air date is not
consulted**, and that is measured rather than assumed: among user-touched
orphans with a unique same-show title match, 34 of 67 carry a non-zero date
delta — up to 5,332 days — and *every one was inspected and is correct*. The two
catalogues date compilations and webisodes differently. Requiring the date costs
real matches (105,484 against 117,264 across the population; 34 user-touched
against 64) and buys nothing.

**Tier 2 — link the show, then translate the exact key across the link.** Some
series exist twice: TMDB models Will & Grace's revival as a **separate series**,
already in the catalog as `1064267`. Establish a link, derive the constant
season offset from the evidence pairs, then pair on
`(season_number − offset, episode_number)`, 1:1 on both sides. **The title is
not consulted in that last step** — `s9e1` is TV Maze's `"Eleven Years Later"`
and TMDB's `"11 Years Later"`, which the fold reconciles for punctuation and
case but not for a spelled-out numeral, so a title-gated rule drops the
revival's premiere while placing all 16 episodes around it. Measured: the offset
key pairs 52 of 57 and **17 of 17 user-touched**, against title+airdate's 48 and
16.

**Tier 2b — folded title alone, within the linked pair.** For a link whose
counterpart is a *season of an anthology* rather than a series, where no
consistent offset exists: TMDB models "Cunk on Earth" as season 2 of "Cunk on…",
and its five episodes title-match uniquely while every air date differs (TV Maze
recorded the Netflix drop, TMDB the weekly BBC broadcasts).

**Tier 3 — no counterpart. Delete.**

Every tier requires 1:1 uniqueness **on both sides** and resolves any ambiguity
to unmatched. No air-date-only matching at any grain; no title-only matching
*across* shows at episode grain — `"Rise of the Machines"` is an episode title
in twelve different series. Ambiguity is never resolved by primary key, in
either direction: two orphans sharing a key and two ingested rows sharing one
are both refused, which is NEU-1126's rule carried over verbatim.

## Two places this module reads the spec's measurements over its prose

Recorded because both look like bugs against a literal reading of §3.

**Tier 2b runs under every link, not only offset-less ones.** §3 introduces it as
the fallback "where no consistent offset exists", but §2.5 measures it at 53
rows while also stating all 130 links yielded a consistent offset — figures that
can only be reconciled if 2b also sweeps up what the offset key left behind
under an offset-bearing link. Both readings satisfy §3's actual constraint,
which is uniqueness on both sides; the narrower one would silently drop 53
matches the spec counted.

**An orphan *show* may be linked on aggregate episode-title evidence.** §3's
candidate set is "ingested shows whose folded show name equals the orphan's",
which resolves Will & Grace and Discretion but cannot reach Cunk on Earth —
`"cunkonearth"` does not equal `"cunkon"`. §2.7 nevertheless requires it to
resolve automatically, citing "all 5 regular episodes title-match uniquely" as
the evidence. So for an orphan show with **no** same-folded-name sibling,
`link_by_episode_titles` asks which single ingested show carries uniquely-titled
counterparts to most of its episodes, requiring a unique winner over at least
two of them and at least half. That is a **show-grain aggregate**, not the
episode-grain title match §3.1 forbids: no individual episode is paired by title
across shows, and a title appearing in twelve series contributes one vote to
twelve shows and decides nothing. It is scoped to orphan shows — two rows in
production — and never runs for a show TMDB already matched.

## What the pass does that NEU-1126 did not

**A collision deletes the redundant row instead of keeping it.** NEU-1126 kept a
copy whose user rows could not move, rather than merge two records into one.
Here the opposite is correct: if a user holds rows on *both* the orphan and its
twin, the twin's row already records that viewing, so the orphan's is redundant.
A user who watched Friends `s4e23` and `s4e24 "Part II"` ends with one row for
what TMDB models as one episode. That is a re-count under TMDB's episode model,
not a loss — and the reconciliation harness has to be told to expect it.

**It creates `app` rows.** When a re-point moves a user's episode into a
*different* show, that show is inserted into their My Shows. Without it the
history is intact by row count and invisible in the product: Watch Next,
progress and the show page all key off the tracked show. Will & Grace is the
live case — 16 watches land on a series nobody tracks. This is the only place
the pass writes an `app` row rather than moving one, and it is an expected
reconciliation **gain**.

**Nothing is kept.** Criterion 7 is that all three tables hold zero
`tmdb_id IS NULL` rows afterwards, so the delete is not conditional on having
found a counterpart. That is why `_DELETE_EPISODES` drops NEU-1126's
`sh.tmdb_id IS NOT NULL` guard — orphans under an unmatched show go too — while
keeping `e.tmdb_id IS NULL`, which is the guard that actually matters and the
one that makes "an ingested row is never deleted" structural rather than a
property of whichever query built the work list.

## Order, and why

Episodes, then seasons, then shows: a season is deletable once it holds no
episodes, a show once it holds no seasons. The two orphan seasons that hold
*ingested* episodes have those episodes re-pointed onto the surviving ingested
season first, exactly as `season_dedupe` does, rather than being deleted out
from under them.

Bulk episode deletion needs `ix_show_last_episode_to_air_id` and
`ix_show_next_episode_to_air_id` (migration `f85a608ef19e`) or the FK checks
seq-scan `catalog.show` per row. They exist. Do not remove them.

## Matching happens in Python over titles Postgres folded

The fold is `sql_fold.folded`, evaluated in Postgres and projected into the
result set — never `unicodedata`, which does not decompose ł, ø, đ or ħ and so
disagrees with the SQL definition on precisely the titles the fold exists for.
Only the *grouping and equality* of already-folded strings happens in Python.
That is one step further than `enrichment.py` goes — it binds both sides into
`folded_equal` and lets Postgres decide the comparison too — and the reason for
the difference is that this matcher asks a question `folded_equal` cannot
express: not "are these two titles equal" but "how many rows in this show share
this title", which is a grouping over a whole result set rather than a pairwise
test. The fold itself is still `sql_fold.folded` and there is still only one of
it. Doing it this way is what lets
`report` and `retire` share one matcher: the report is the reviewable artifact
for a pass that destroys data, so "the report predicted what the pass did" has
to be structural rather than two queries maintained in parallel.

Work is per show because every tier is show-scoped, which also bounds memory and
makes each tier directly testable.

## Re-runnability

There is no watermark — a row leaves the work list by being re-pointed or
deleted. Like `season_dedupe`, **re-run this after any later ingest or delta**: a
delta can add an ingested episode that gives a surviving orphan a twin, and
tier 0's 880 rows are that mechanism already observed. Idempotent; a failed show
rolls back to the last commit and a re-run starts from the beginning, finding
only what is genuinely still there.

**It is not reversible.** The pre-drop `tvmaze` dump is the only source for the
deleted catalog rows, and it cannot restore `app` rows at all. What can is
`app.watch_archive`, which holds a human-readable snapshot of every watch and
rating, carries no foreign keys, and survives everything.
"""

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import Text, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tvbf.app.models import UserShowWatch
from tvbf.catalog import models as cm
from tvbf.sql_fold import folded
from tvbf.tmdb import user_history
from tvbf.tmdb.episode_repoint import (
    MIN_INGESTED_SHOWS as MIN_INGESTED_SHOWS,
)
from tvbf.tmdb.episode_repoint import (
    IngestNotRun as IngestNotRun,
)
from tvbf.tmdb.episode_repoint import (
    ingested_show_count,
)

log = logging.getLogger(__name__)

# Shows per progress log line. The unit of work is a show, and the unit of
# transaction is a show — a failure costs one show's writes, and the re-run
# finds exactly what is left.
_LOG_EVERY = 500

# How much aggregate episode-title agreement links an orphan show to an ingested
# one when no same-folded-name sibling exists. Two thresholds, both deliberately
# blunt: a unique winner over at least this many of the orphan's distinct titles,
# and at least this share of the ones that matched anything at all. Cunk on Earth
# scores 5 of 5 against a field where a title like "Rise of the Machines" hands
# single votes to eleven unrelated series.
_LINK_MIN_TITLE_VOTES = 2
_LINK_MIN_TITLE_SHARE = 0.5

# Tier labels. Strings rather than an enum because they are dictionary keys in a
# JSON report read in a terminal, and their names are the spec's.
TIER_EXACT_KEY = "tier0_exact_key"
TIER_SAME_SHOW_TITLE = "tier1_same_show_unique_title"
TIER_LINK_OFFSET_KEY = "tier2_link_offset_key"
TIER_LINK_TITLE = "tier2b_link_title_fallback"
TIERS = (TIER_EXACT_KEY, TIER_SAME_SHOW_TITLE, TIER_LINK_OFFSET_KEY, TIER_LINK_TITLE)

# Why an orphan exists — the spec's §2.3 causes, and the report's first
# breakdown. Assigned by `classify_cause` in a fixed order: structural facts
# about the show first, then what the exact key found, then the row's own shape,
# then where it sits in the season. The order is a tie-break between overlapping
# descriptions of one row, not a claim that only one applies.
CAUSE_SHOW_UNMATCHED = "show_unmatched"
CAUSE_SHOW_NO_INGESTED_EPISODES = "show_matched_zero_episodes_ingested"
CAUSE_EXACT_TWIN = "exact_twin_1_to_1"
CAUSE_AMBIGUOUS_KEY = "ambiguous_one_twin_multiple_copies"
CAUSE_SYNTHETIC_SPECIAL = "synthetic_special_negative_number"
CAUSE_SEASON_ABSENT = "season_absent_from_tmdb"
CAUSE_PAST_END_OF_SEASON = "number_past_end_of_tmdb_season"
CAUSE_GAP_IN_SEASON = "gap_inside_season_tmdb_covers"

# Why an orphan reached tier 3, never folded into one "unmatched" bucket. Read
# off the broadest tier that could have taken it — the same-show title rule —
# because that is the one whose failure is informative.
REJECT_BLANK_TITLE = "blank_title_after_folding"
REJECT_AMBIGUOUS_ORPHAN_SIDE = "ambiguous_two_or_more_orphans_share_the_title"
REJECT_AMBIGUOUS_INGESTED_SIDE = "ambiguous_two_or_more_ingested_share_the_title"
REJECT_NO_COUNTERPART = "no_ingested_episode_with_that_title_in_the_show"

# How a show link was established.
LINK_TITLE_DATE_EVIDENCE = "title_and_airdate_evidence"
LINK_SOLE_SIBLING = "sole_same_folded_name_sibling"
LINK_EPISODE_TITLE_AGGREGATE = "aggregate_episode_title_evidence"

# Why a deleted user row was deleted — the two dispositions §5 requires the loss
# list to keep apart. Folding them together would report ~109 losses where ~95
# are real.
LOSS_GENUINE = "genuine_loss"
LOSS_DEDUPLICATION = "deduplication"


class OrphanRetireAborted(Exception):
    """A show's writes did not account for every row selected. The message is what to read."""


@dataclass(frozen=True)
class EpisodeRow:
    """One `catalog.episode`, with its title folded by Postgres."""

    id: int
    show_id: int
    season_number: int
    episode_number: int
    name: str | None
    folded_name: str
    air_date: date | None


@dataclass(frozen=True)
class ShowLink:
    """An orphan-bearing show and the ingested show its leftovers belong to."""

    from_show_id: int
    to_show_id: int
    season_offset: int | None
    evidence: int
    basis: str


@dataclass(frozen=True)
class LinkResolution:
    """A link decision and the evidence count behind it.

    `candidates` is how many sibling shows carried evidence. More than one is
    the case §2.5 counts separately — four shows in production — and it is a
    refusal, not a tie to be broken, so it has to survive as a number rather
    than as a `None` indistinguishable from "no sibling at all".
    """

    link: "ShowLink | None"
    candidates: int


@dataclass(frozen=True)
class EpisodeMatch:
    """One orphan episode and the ingested row that supersedes it."""

    orphan_id: int
    twin_id: int
    twin_show_id: int
    tier: str


@dataclass(frozen=True)
class ShowPlan:
    """Everything the pass would do to one show, decided before anything is written.

    `deletions` is every orphan episode with no counterpart; `matches` is every
    one with a counterpart. Their union is all of the show's orphans, because
    nothing is kept — see the module docstring.
    """

    show_id: int
    show_tmdb_id: int | None
    link: ShowLink | None
    link_candidates: int
    matches: tuple[EpisodeMatch, ...]
    deletions: tuple[int, ...]
    causes: Counter[str] = field(default_factory=Counter)
    rejections: Counter[str] = field(default_factory=Counter)
    orphans_by_id: dict[int, EpisodeRow] = field(default_factory=dict)
    ingested_by_id: dict[int, EpisodeRow] = field(default_factory=dict)

    @property
    def orphan_count(self) -> int:
        return len(self.matches) + len(self.deletions)


# --------------------------------------------------------------------------
# The matcher. Pure functions over rows Postgres already folded, so every tier
# is directly testable and the report cannot drift from the pass.
# --------------------------------------------------------------------------


def _by_key(rows: Iterable[EpisodeRow]) -> dict[tuple[int, int], list[EpisodeRow]]:
    out: dict[tuple[int, int], list[EpisodeRow]] = defaultdict(list)
    for row in rows:
        out[(row.season_number, row.episode_number)].append(row)
    return out


def _by_fold(rows: Iterable[EpisodeRow]) -> dict[str, list[EpisodeRow]]:
    """Grouped by folded title. **A title that folds to nothing never matches.**

    "!!!" and "???" both fold to the empty string; treating those as the same
    title is the one way this could pair two unrelated episodes, which is the
    rule `folded_equal` already draws.
    """
    out: dict[str, list[EpisodeRow]] = defaultdict(list)
    for row in rows:
        if row.folded_name:
            out[row.folded_name].append(row)
    return out


def classify_cause(
    orphan: EpisodeRow,
    *,
    show_matched: bool,
    ingested: Sequence[EpisodeRow],
    orphans_by_key: dict[tuple[int, int], list[EpisodeRow]],
    ingested_by_key: dict[tuple[int, int], list[EpisodeRow]],
) -> str:
    """Which of §2.3's causes best describes why this row never paired on the key."""
    if not show_matched:
        return CAUSE_SHOW_UNMATCHED
    if not ingested:
        return CAUSE_SHOW_NO_INGESTED_EPISODES

    key = (orphan.season_number, orphan.episode_number)
    twins = ingested_by_key.get(key, ())
    if twins:
        if len(twins) == 1 and len(orphans_by_key.get(key, ())) == 1:
            return CAUSE_EXACT_TWIN
        return CAUSE_AMBIGUOUS_KEY

    if orphan.episode_number < 0:
        return CAUSE_SYNTHETIC_SPECIAL

    in_season = [e for e in ingested if e.season_number == orphan.season_number]
    if not in_season:
        return CAUSE_SEASON_ABSENT
    if orphan.episode_number > max(e.episode_number for e in in_season):
        return CAUSE_PAST_END_OF_SEASON
    return CAUSE_GAP_IN_SEASON


def classify_rejection(
    orphan: EpisodeRow,
    *,
    orphans_by_fold: dict[str, list[EpisodeRow]],
    ingested_by_fold: dict[str, list[EpisodeRow]],
) -> str:
    """Why tier 1 — the broadest tier — declined this row."""
    if not orphan.folded_name:
        return REJECT_BLANK_TITLE
    if len(orphans_by_fold.get(orphan.folded_name, ())) > 1:
        return REJECT_AMBIGUOUS_ORPHAN_SIDE
    twins = ingested_by_fold.get(orphan.folded_name, ())
    if len(twins) > 1:
        return REJECT_AMBIGUOUS_INGESTED_SIDE
    return REJECT_NO_COUNTERPART


def _is_special(row: EpisodeRow) -> bool:
    """Either kind of special, decided the way `catalog/episodes.py` decides it.

    TMDB's season 0, or the negative `episode_number` NEU-1042 invented for a
    TV Maze special. The SQL definitions are `IS_SPECIAL` / `IS_COPIED_SPECIAL`
    there; this is the row-object half, kept in step with them.
    """
    return row.season_number == 0 or row.episode_number < 0


def _evidence_pairs(
    orphans: Sequence[EpisodeRow], candidates: Sequence[EpisodeRow]
) -> list[tuple[EpisodeRow, EpisodeRow]]:
    """Orphan/candidate pairs agreeing on folded title **and** exact air date, 1:1.

    Used only to establish a show link and derive its season offset, never to
    pair an episode on its own — the conjunction is what keeps a candidate set
    bounded by show name from admitting a coincidence. Pairs that are ambiguous
    on either side are dropped rather than resolved, so an ambiguous pair can
    neither create a link nor corrupt the offset it implies.
    """
    orphan_pairs = Counter(
        (o.folded_name, o.air_date) for o in orphans if o.folded_name and o.air_date
    )
    candidate_pairs: dict[tuple[str, date], list[EpisodeRow]] = defaultdict(list)
    for row in candidates:
        if row.folded_name and row.air_date:
            candidate_pairs[(row.folded_name, row.air_date)].append(row)

    pairs = []
    for orphan in orphans:
        if not orphan.folded_name or orphan.air_date is None:
            continue
        key = (orphan.folded_name, orphan.air_date)
        if orphan_pairs[key] != 1:
            continue
        matches = candidate_pairs.get(key, ())
        if len(matches) == 1:
            pairs.append((orphan, matches[0]))
    return pairs


def resolve_link(
    *,
    from_show_id: int,
    orphans: Sequence[EpisodeRow],
    siblings: Sequence[int],
    sibling_episodes: Mapping[int, Sequence[EpisodeRow]],
) -> LinkResolution:
    """Link an orphan-bearing show to the one ingested show its leftovers belong to.

    The candidate set is ingested shows whose **folded show name equals** this
    one's, which is loose on its own — "Lost" has four same-named siblings,
    "Friends" seven — so the episode-grain conjunction of title *and* exact air
    date does the real filtering. Run across all same-show misses it returned
    sixteen matches, every one Will & Grace, and no false positive anywhere.

    Two routes in. Evidence decides when there is any: exactly one sibling with
    evidence links, **more than one links nothing** — four such shows in
    production, refused rather than scored. With no evidence at all a lone
    sibling still links, which is the only thing that can reach a show carrying
    no episodes to produce evidence from (Discretion).

    The offset is required to be **consistent** across the evidence pairs that
    can speak to it — which excludes specials, for the reason given at the
    computation. Nothing anywhere resolves a genuinely inconsistent offset by
    taking the commonest: an anthology counterpart legitimately has none, and
    tier 2b is what handles that.
    """
    with_evidence: list[tuple[int, list[tuple[EpisodeRow, EpisodeRow]]]] = []
    for sibling in siblings:
        pairs = _evidence_pairs(orphans, list(sibling_episodes.get(sibling, ())))
        if pairs:
            with_evidence.append((sibling, pairs))

    if len(with_evidence) > 1:
        return LinkResolution(link=None, candidates=len(with_evidence))

    if len(with_evidence) == 1:
        to_show_id, pairs = with_evidence[0]
        # **Specials are excluded from the offset, and this is not a tolerance
        # for outliers.** NEU-1042 numbered a TV Maze special negative *within
        # its original season* while TMDB parks specials in season 0, so a
        # special's season relationship is precisely the one that does not
        # follow the series'. Feeding it in is a category error, and one such
        # pair is enough to make a unanimous offset look inconsistent.
        #
        # Measured against production 2026-08-14, Will & Grace: 47 evidence
        # pairs give offset 8 and one — orphan `s11e-1` against TMDB's `s0e1` —
        # gives 11. Including it collapsed the offset to `None`, dropped the
        # show to the title fallback, and rescued 16 of 17 user-touched rows.
        # That is the exact outcome the acceptance criteria name as a failure.
        # Excluding specials leaves `{8}` unanimous, so the consistency rule
        # stays strict — nothing here takes the commonest offset.
        offsets = {
            o.season_number - t.season_number
            for o, t in pairs
            if not _is_special(o) and not _is_special(t)
        }
        return LinkResolution(
            link=ShowLink(
                from_show_id=from_show_id,
                to_show_id=to_show_id,
                season_offset=offsets.pop() if len(offsets) == 1 else None,
                evidence=len(pairs),
                basis=LINK_TITLE_DATE_EVIDENCE,
            ),
            candidates=1,
        )

    if len(siblings) == 1:
        return LinkResolution(
            link=ShowLink(
                from_show_id=from_show_id,
                to_show_id=siblings[0],
                season_offset=None,
                evidence=0,
                basis=LINK_SOLE_SIBLING,
            ),
            candidates=1,
        )
    return LinkResolution(link=None, candidates=0)


def link_by_episode_titles(
    *,
    from_show_id: int,
    orphans: Sequence[EpisodeRow],
    titles_elsewhere: dict[str, list[tuple[int, int]]],
) -> ShowLink | None:
    """Link an orphan **show** on aggregate episode-title agreement.

    The route of last resort, for an orphan show with no same-folded-name
    sibling — TMDB models "Cunk on Earth" as a season of "Cunk on…", so no
    show-name rule can reach it, and §2.7 requires it to resolve without a human.

    `titles_elsewhere` maps a folded episode title to the `(show_id, episode_id)`
    pairs carrying it anywhere in the catalog. A title is counted for a show only
    when that show holds **exactly one** episode with it, so an ambiguous title
    contributes nothing rather than a coin flip. The winner must be unique, must
    take at least `_LINK_MIN_TITLE_VOTES` of this show's distinct titles, and
    must take at least `_LINK_MIN_TITLE_SHARE` of the titles that matched
    anything.

    **This is a show-grain aggregate and not the cross-show title matching §3.1
    forbids.** No episode is paired here — the link it returns still has to place
    every episode through tier 2 or 2b, each of which demands uniqueness on both
    sides within the linked pair. A title occurring in twelve unrelated series
    hands each of them one vote and decides nothing.
    """
    votes: Counter[int] = Counter()
    distinct_titles = {o.folded_name for o in orphans if o.folded_name}
    for title in distinct_titles:
        per_show: Counter[int] = Counter(show for show, _ in titles_elsewhere.get(title, ()))
        for show_id, count in per_show.items():
            if show_id != from_show_id and count == 1:
                votes[show_id] += 1
    if not votes:
        return None

    ranked = votes.most_common()
    best_show, best_votes = ranked[0]
    if len(ranked) > 1 and ranked[1][1] == best_votes:
        return None
    matched_titles = sum(1 for title in distinct_titles if titles_elsewhere.get(title))
    if best_votes < _LINK_MIN_TITLE_VOTES:
        return None
    if matched_titles and best_votes < matched_titles * _LINK_MIN_TITLE_SHARE:
        return None

    return ShowLink(
        from_show_id=from_show_id,
        to_show_id=best_show,
        season_offset=None,
        evidence=best_votes,
        basis=LINK_EPISODE_TITLE_AGGREGATE,
    )


def match_episodes(
    *,
    orphans: Sequence[EpisodeRow],
    ingested: Sequence[EpisodeRow],
    link: ShowLink | None = None,
    linked_episodes: Sequence[EpisodeRow] = (),
) -> list[EpisodeMatch]:
    """Apply the four tiers in order, first hit wins, to one show's orphans.

    Uniqueness on the orphan side is counted across **all** of the show's
    orphans, not just the ones still unplaced — a key carrying two orphans is
    ambiguous whether or not one of them has already been placed by an earlier
    tier, and counting only the remainder would make an ambiguous pair look
    unique as soon as its first half moved.
    """
    matches: list[EpisodeMatch] = []
    placed: set[int] = set()
    claimed: set[int] = set()

    orphans_by_key = _by_key(orphans)
    ingested_by_key = _by_key(ingested)
    orphans_by_fold = _by_fold(orphans)
    ingested_by_fold = _by_fold(ingested)

    def take(orphan: EpisodeRow, twin: EpisodeRow, twin_show_id: int, tier: str) -> None:
        """Claim a twin for an orphan, unless an earlier tier already claimed it.

        Uniqueness *within* a tier is guaranteed by that tier's own rules, but
        **nothing stops two tiers landing on one twin**: an orphan can pair on
        the exact key while a second pairs on that same ingested episode's
        title. Left alone that merges two distinct episodes onto one row and the
        user rows follow it — the first orphan's watch moves, the second
        collides on `(user_id, episode_id)` and is deleted as redundant. That is
        a real watch record lost to exactly the ambiguity §3 says must resolve to
        unmatched, and the report would have predicted both moving.

        The earlier tier keeps the twin, because the tiers are ordered by how
        much evidence they stand on and §3 applies them "in order, each stopping
        at the first hit". The loser is not re-tried against a weaker twin: it
        falls through to tier 3, which is the refusal every other ambiguity gets.
        """
        if twin.id in claimed:
            return
        matches.append(
            EpisodeMatch(orphan_id=orphan.id, twin_id=twin.id, twin_show_id=twin_show_id, tier=tier)
        )
        placed.add(orphan.id)
        claimed.add(twin.id)

    # Tier 0 — the exact key, 1:1 both sides.
    for orphan in orphans:
        key = (orphan.season_number, orphan.episode_number)
        twins = ingested_by_key.get(key, ())
        if len(twins) == 1 and len(orphans_by_key[key]) == 1:
            take(orphan, twins[0], twins[0].show_id, TIER_EXACT_KEY)

    # Tier 1 — the folded title, unique on both sides within the show.
    for orphan in orphans:
        if orphan.id in placed or not orphan.folded_name:
            continue
        if len(orphans_by_fold[orphan.folded_name]) != 1:
            continue
        twins = ingested_by_fold.get(orphan.folded_name, ())
        if len(twins) == 1:
            take(orphan, twins[0], twins[0].show_id, TIER_SAME_SHOW_TITLE)

    if link is None or not linked_episodes:
        return matches

    linked_by_key = _by_key(linked_episodes)
    linked_by_fold = _by_fold(linked_episodes)

    # Tier 2 — the exact key with a constant translation, title not consulted.
    if link.season_offset is not None:
        for orphan in orphans:
            if orphan.id in placed:
                continue
            own_key = (orphan.season_number, orphan.episode_number)
            if len(orphans_by_key[own_key]) != 1:
                continue
            key = (orphan.season_number - link.season_offset, orphan.episode_number)
            twins = linked_by_key.get(key, ())
            if len(twins) == 1:
                take(orphan, twins[0], link.to_show_id, TIER_LINK_OFFSET_KEY)

    # Tier 2b — folded title alone, inside the linked pair.
    for orphan in orphans:
        if orphan.id in placed or not orphan.folded_name:
            continue
        if len(orphans_by_fold[orphan.folded_name]) != 1:
            continue
        twins = linked_by_fold.get(orphan.folded_name, ())
        if len(twins) == 1:
            take(orphan, twins[0], link.to_show_id, TIER_LINK_TITLE)

    return matches


# --------------------------------------------------------------------------
# Reading the catalog. Titles are folded by Postgres and projected out; see the
# module docstring for why the comparison then happens in Python.
# --------------------------------------------------------------------------


def _folded(column) -> Any:
    """`sql_fold.folded` over a column that may be NULL.

    A NULL title folds to NULL, which would compare unequal to everything
    including itself and quietly leave blank-title rows out of the groups the
    rejection reasons are read off. Coalescing first makes them the empty
    string, which `_by_fold` already refuses to match on.
    """
    return folded(func.coalesce(column, literal("", Text)))


_EPISODE_COLUMNS = (
    cm.Episode.id,
    cm.Episode.show_id,
    cm.Episode.season_number,
    cm.Episode.episode_number,
    cm.Episode.name,
    cm.Episode.air_date,
    cm.Episode.tmdb_id,
)


def _episode_row(row) -> EpisodeRow:
    return EpisodeRow(
        id=row.id,
        show_id=row.show_id,
        season_number=row.season_number,
        episode_number=row.episode_number,
        name=row.name,
        folded_name=row.folded_name or "",
        air_date=row.air_date,
    )


async def _show_episodes(
    db: AsyncSession, show_id: int
) -> tuple[list[EpisodeRow], list[EpisodeRow]]:
    """One show's episodes, split into orphans and ingested rows. One query."""
    stmt = select(*_EPISODE_COLUMNS, _folded(cm.Episode.name).label("folded_name")).where(
        cm.Episode.show_id == show_id
    )
    orphans: list[EpisodeRow] = []
    ingested: list[EpisodeRow] = []
    for row in (await db.execute(stmt)).all():
        (orphans if row.tmdb_id is None else ingested).append(_episode_row(row))
    return orphans, ingested


async def _ingested_episodes(db: AsyncSession, show_id: int) -> list[EpisodeRow]:
    stmt = select(*_EPISODE_COLUMNS, _folded(cm.Episode.name).label("folded_name")).where(
        cm.Episode.show_id == show_id, cm.Episode.tmdb_id.is_not(None)
    )
    return [_episode_row(row) for row in (await db.execute(stmt)).all()]


async def _work_shows(db: AsyncSession) -> list[Any]:
    """Every show holding an orphan episode, plus every orphan show, in id order.

    An orphan show with no orphan episodes still belongs here — it has user rows
    to move and a row of its own to delete.
    """
    orphan_episode_exists = (
        select(literal(1))
        .where(cm.Episode.show_id == cm.Show.id, cm.Episode.tmdb_id.is_(None))
        .exists()
    )
    stmt = (
        select(
            cm.Show.id,
            cm.Show.name,
            cm.Show.tmdb_id,
            _folded(cm.Show.name).label("folded_name"),
        )
        .where(or_(cm.Show.tmdb_id.is_(None), orphan_episode_exists))
        .order_by(cm.Show.id)
    )
    return list((await db.execute(stmt)).all())


async def _ingested_shows_by_folded_name(db: AsyncSession) -> dict[str, list[int]]:
    """Every ingested show, indexed by folded name — the tier 2 candidate set.

    Built once in Python rather than probed per show: `ix_show_name_folded_trgm`
    is a trigram index built for `LIKE`, and 229,000 shows re-folded for each of
    ~13,000 orphan-bearing shows is the one thing that would make this pass
    unaffordable. The same trade the probe SQL made with a temp table.
    """
    stmt = select(cm.Show.id, _folded(cm.Show.name).label("folded_name")).where(
        cm.Show.tmdb_id.is_not(None)
    )
    index: dict[str, list[int]] = defaultdict(list)
    for row in (await db.execute(stmt)).all():
        if row.folded_name:
            index[row.folded_name].append(row.id)
    return index


async def _episodes_titled(
    db: AsyncSession, titles: Sequence[str]
) -> dict[str, list[tuple[int, int]]]:
    """Where else in the catalog these folded episode titles occur.

    The one query in this module with no index behind it — `catalog.episode` has
    no expression index on the folded name, so this is a sequential scan of ~7.3M
    rows. It runs **only** for an orphan show with no same-folded-name sibling
    (one in production) and never for a matched show, which is what keeps it to a
    handful of scans across the whole pass rather than one per show.
    """
    if not titles:
        return {}
    folded_name = _folded(cm.Episode.name)
    stmt = select(cm.Episode.show_id, cm.Episode.id, folded_name.label("folded_name")).where(
        cm.Episode.tmdb_id.is_not(None), folded_name.in_(list(titles))
    )
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in (await db.execute(stmt)).all():
        out[row.folded_name].append((row.show_id, row.id))
    return out


class _Catalog:
    """Per-run caches over the reads the planner makes repeatedly.

    A linked show's episodes are fetched once however many orphan shows point at
    it, which matters because a handful of ingested shows absorb the long tail of
    links.
    """

    def __init__(
        self,
        db: AsyncSession,
        siblings_by_name: dict[str, list[int]],
        *,
        ingest_ran: bool,
    ) -> None:
        self._db = db
        self._siblings_by_name = siblings_by_name
        self._episodes: dict[int, list[EpisodeRow]] = {}
        # Whether the aggregate-title link route is worth its cost — see
        # `may_search_titles`.
        self.ingest_ran = ingest_ran

    @property
    def may_search_titles(self) -> bool:
        """Whether to attempt the last-resort link route at all.

        That route seq-scans `catalog.episode` (no expression index on the
        folded name), which is affordable because it runs once per orphan show
        with no same-named sibling — **one** show in production.

        Before the ingest has run that premise inverts and the cost becomes
        unbounded: on a mid-migration database almost every show is still an
        orphan, so a pre-ingest run would attempt tens of thousands of scans.
        Measured on the dev box on 2026-08-14: 88,925 orphan shows against 251
        ingested ones. It would also be pointless — the route searches *ingested*
        episodes, and below the floor there are barely any to find — so this is
        not a speed/accuracy trade. `report` deliberately carries no floor of its
        own, because it is the half you run to decide, which is exactly why it
        needs this guard rather than inheriting `retire`'s refusal.
        """
        return self.ingest_ran

    def siblings(self, folded_name: str, exclude: int) -> list[int]:
        if not folded_name:
            return []
        return [s for s in self._siblings_by_name.get(folded_name, ()) if s != exclude]

    async def ingested_episodes(self, show_id: int) -> list[EpisodeRow]:
        if show_id not in self._episodes:
            self._episodes[show_id] = await _ingested_episodes(self._db, show_id)
        return self._episodes[show_id]


async def build_show_plan(db: AsyncSession, show, catalog: _Catalog) -> ShowPlan:
    """Decide every disposition for one show before a single row is written.

    `show` is a row carrying `id`, `name`, `tmdb_id` and `folded_name`. The plan
    it returns is what both `report` and `retire` consume, which is the property
    that keeps the reviewed artifact and the executed pass from drifting apart.
    """
    orphans, ingested = await _show_episodes(db, show.id)

    link: ShowLink | None = None
    link_candidates = 0
    linked_episodes: list[EpisodeRow] = []
    siblings = catalog.siblings(show.folded_name, exclude=show.id)
    if siblings:
        sibling_episodes = {s: await catalog.ingested_episodes(s) for s in siblings}
        resolution = resolve_link(
            from_show_id=show.id,
            orphans=orphans,
            siblings=siblings,
            sibling_episodes=sibling_episodes,
        )
        link, link_candidates = resolution.link, resolution.candidates
    elif show.tmdb_id is None and orphans and catalog.may_search_titles:
        # Route of last resort, orphan shows only — see `link_by_episode_titles`.
        titles = sorted({o.folded_name for o in orphans if o.folded_name})
        link = link_by_episode_titles(
            from_show_id=show.id,
            orphans=orphans,
            titles_elsewhere=await _episodes_titled(db, titles),
        )
    if link is not None:
        linked_episodes = await catalog.ingested_episodes(link.to_show_id)

    matches = match_episodes(
        orphans=orphans,
        ingested=ingested,
        link=link,
        linked_episodes=linked_episodes,
    )

    placed = {m.orphan_id for m in matches}
    orphans_by_key = _by_key(orphans)
    ingested_by_key = _by_key(ingested)
    orphans_by_fold = _by_fold(orphans)
    ingested_by_fold = _by_fold(ingested)

    causes: Counter[str] = Counter()
    rejections: Counter[str] = Counter()
    for orphan in orphans:
        causes[
            classify_cause(
                orphan,
                show_matched=show.tmdb_id is not None,
                ingested=ingested,
                orphans_by_key=orphans_by_key,
                ingested_by_key=ingested_by_key,
            )
        ] += 1
        if orphan.id not in placed:
            rejections[
                classify_rejection(
                    orphan,
                    orphans_by_fold=orphans_by_fold,
                    ingested_by_fold=ingested_by_fold,
                )
            ] += 1

    return ShowPlan(
        show_id=show.id,
        show_tmdb_id=show.tmdb_id,
        link=link,
        link_candidates=link_candidates,
        matches=tuple(matches),
        deletions=tuple(o.id for o in orphans if o.id not in placed),
        causes=causes,
        rejections=rejections,
        orphans_by_id={o.id: o for o in orphans},
        ingested_by_id={e.id: e for e in ingested},
    )


# --------------------------------------------------------------------------
# Writing. Every statement re-asserts the predicate that makes it safe rather
# than trusting the query that built its work list — the stance `season_dedupe`
# and `episode_repoint` both take, and for the same reason: these are the
# statements that destroy data no feed can restore.
# --------------------------------------------------------------------------

# No withholding policy: unlike `episode_repoint`, nothing here is kept, so a
# row that cannot move is deleted rather than left behind (§4.2).
_EPISODE_WRITES = user_history.episode_statements()
_SHOW_WRITES = user_history.show_statements()

_DELETE_EPISODE_WATCHES = text(
    "DELETE FROM app.user_episode_watch WHERE episode_id = ANY(cast(:orphans AS bigint[]))"
)
_DELETE_EPISODE_RATINGS = text(
    "DELETE FROM app.user_episode_rating WHERE episode_id = ANY(cast(:orphans AS bigint[]))"
)
_DELETE_EPISODE_EVENTS = text(
    "DELETE FROM app.activity_event WHERE target_type = 'episode' "
    "AND target_id = ANY(cast(:orphans AS bigint[]))"
)

# `e.tmdb_id IS NULL` is the guard that matters and it is repeated here on
# purpose. NEU-1126's `sh.tmdb_id IS NOT NULL` is deliberately **not** carried:
# an orphan under an unmatched show has to go too, or criterion 7 cannot hold.
# `NOT (still referenced)` stays as a post-condition — the user rows were
# deleted or moved moments ago, so a row failing it means the two disagree, and
# the caller aborts rather than stepping past it.
_DELETE_EPISODES = text(f"""
    DELETE FROM catalog.episode e
     WHERE e.id = ANY(cast(:orphans AS bigint[]))
       AND e.tmdb_id IS NULL
       AND NOT ({user_history.EPISODE_STILL_REFERENCED})
""")

# The episodes of a doomed season move to the ingested season carrying the same
# number, and `t.n = 1` refuses to pick between two. Identical in shape and
# reasoning to `season_dedupe._REPOINT`, which is the pass that established that
# a season's episodes are re-pointed *before* it is deleted rather than left to
# `ON DELETE SET NULL`.
_REPOINT_SEASON_EPISODES = text("""
    UPDATE catalog.episode e
       SET season_id = t.id
      FROM catalog.season d
     CROSS JOIN LATERAL (
             SELECT min(s.id) AS id, count(*) AS n
               FROM catalog.season s
              WHERE s.show_id = d.show_id
                AND s.season_number = d.season_number
                AND s.tmdb_id IS NOT NULL
           ) t
     WHERE d.id = ANY(cast(:doomed AS bigint[]))
       AND e.season_id = d.id
       AND t.n = 1
""")

_EPISODES_ON_SEASONS = text(
    "SELECT count(*) FROM catalog.episode WHERE season_id = ANY(cast(:doomed AS bigint[]))"
)

_DELETE_SEASONS = text(
    "DELETE FROM catalog.season s WHERE s.id = ANY(cast(:doomed AS bigint[])) AND s.tmdb_id IS NULL"
)

_DELETE_SHOW_WATCHES = text(
    "DELETE FROM app.user_show_watch WHERE show_id = ANY(cast(:shows AS bigint[]))"
)
_DELETE_SHOW_RATINGS = text(
    "DELETE FROM app.user_show_rating WHERE show_id = ANY(cast(:shows AS bigint[]))"
)
_DELETE_SHOW_EVENTS = text(
    "DELETE FROM app.activity_event WHERE target_type = 'show' "
    "AND target_id = ANY(cast(:shows AS bigint[]))"
)

# A show whose episodes or seasons have not all gone yet. `catalog.episode` and
# `catalog.season` both CASCADE from `catalog.show`, so deleting such a show
# would take ingested rows with it silently — the one way this pass could
# destroy TMDB data. Checked rather than assumed, even though the episode and
# season phases run first.
_SHOWS_WITH_CHILDREN = text("""
    SELECT s.id FROM catalog.show s
     WHERE s.id = ANY(cast(:shows AS bigint[]))
       AND (EXISTS (SELECT 1 FROM catalog.episode e WHERE e.show_id = s.id)
         OR EXISTS (SELECT 1 FROM catalog.season x WHERE x.show_id = s.id))
""")

# `import_ne.show_resolution` references `catalog.show` with **NO ACTION** (522
# rows in production), so a referenced show cannot be deleted and the attempt
# would abort the transaction. The staging rows are an import audit trail, not
# ours to rewrite, so such a show is skipped and reported. The schema is created
# by the Next Episode import rather than by `db:init`, so its absence is normal
# and `to_regclass` is how that is asked without raising.
_IMPORT_NE_TABLE = text("SELECT to_regclass('import_ne.show_resolution')")
_IMPORT_NE_REFERENCES = text(
    "SELECT DISTINCT show_id FROM import_ne.show_resolution "
    "WHERE show_id = ANY(cast(:shows AS bigint[]))"
)

_DELETE_SHOWS = text(f"""
    DELETE FROM catalog.show s
     WHERE s.id = ANY(cast(:shows AS bigint[]))
       AND s.tmdb_id IS NULL
       AND NOT ({user_history.SHOW_STILL_REFERENCED})
""")

_ORPHAN_SEASONS = text("""
    SELECT id FROM catalog.season WHERE tmdb_id IS NULL ORDER BY id
""")

_ORPHAN_SHOWS = text("""
    SELECT id FROM catalog.show WHERE tmdb_id IS NULL ORDER BY id
""")

_COUNT_ORPHANS = text("""
    SELECT (SELECT count(*) FROM catalog.episode WHERE tmdb_id IS NULL) AS episodes,
           (SELECT count(*) FROM catalog.season WHERE tmdb_id IS NULL) AS seasons,
           (SELECT count(*) FROM catalog.show WHERE tmdb_id IS NULL) AS shows
""")

# Seasons per transaction. Same sizing rationale as `season_dedupe`'s.
SEASON_BATCH_SIZE = 500


@dataclass(frozen=True)
class RetireResult:
    """What one run actually did, at all three grains."""

    shows_planned: int
    episodes_deleted: int
    watches_moved: int
    ratings_moved: int
    activity_moved: int
    watches_deleted: int
    ratings_deleted: int
    activity_deleted: int
    show_watches_created: int
    seasons_deleted: int
    episodes_repointed_to_ingested_season: int
    episodes_left_without_season: int
    shows_deleted: int
    show_watches_moved: int
    show_ratings_moved: int
    show_activity_moved: int
    show_watches_deleted: int
    show_ratings_deleted: int
    show_activity_deleted: int
    shows_kept_referenced: tuple[int, ...]
    links_used: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "shows_planned": self.shows_planned,
            "episodes_deleted": self.episodes_deleted,
            "watches_moved": self.watches_moved,
            "ratings_moved": self.ratings_moved,
            "activity_moved": self.activity_moved,
            "watches_deleted": self.watches_deleted,
            "ratings_deleted": self.ratings_deleted,
            "activity_deleted": self.activity_deleted,
            "show_watches_created": self.show_watches_created,
            "seasons_deleted": self.seasons_deleted,
            "episodes_repointed_to_ingested_season": (self.episodes_repointed_to_ingested_season),
            "episodes_left_without_season": self.episodes_left_without_season,
            "shows_deleted": self.shows_deleted,
            "show_watches_moved": self.show_watches_moved,
            "show_ratings_moved": self.show_ratings_moved,
            "show_activity_moved": self.show_activity_moved,
            "show_watches_deleted": self.show_watches_deleted,
            "show_ratings_deleted": self.show_ratings_deleted,
            "show_activity_deleted": self.show_activity_deleted,
            "shows_kept_referenced": list(self.shows_kept_referenced),
            "links_used": [dict(link) for link in self.links_used],
        }


async def _assert_ingest_ran(db: AsyncSession, floor: int) -> None:
    ingested = await ingested_show_count(db)
    if ingested < floor:
        raise IngestNotRun(
            f"{ingested} show(s) carry a tmdb_synced_at, under the floor of "
            f"{floor} — run the full TMDB catalog ingest first, or almost no "
            f"orphan has a counterpart and this pass would delete the lot"
        )


async def _apply_episode_plan(db: AsyncSession, plan: ShowPlan) -> dict[str, int]:
    """Move what moves, delete what does not, then delete every orphan. One transaction.

    The order is load-bearing. User rows move first, so a row that *could* move
    is never counted as a loss; the leftovers are deleted next, which is what
    the twin already recording that viewing licenses (§4.2); and only then are
    the catalog rows deleted, with `NOT (still referenced)` re-asserted so a
    disagreement between the two aborts the show instead of destroying a watch.
    """
    orphan_ids = [m.orphan_id for m in plan.matches] + list(plan.deletions)
    if not orphan_ids:
        return {}

    counts: dict[str, int] = dict.fromkeys(
        (
            "watches_moved",
            "ratings_moved",
            "activity_moved",
            "watches_deleted",
            "ratings_deleted",
            "activity_deleted",
            "show_watches_created",
            "episodes_deleted",
        ),
        0,
    )

    if plan.matches:
        doomed = [m.orphan_id for m in plan.matches]
        survivors = [m.twin_id for m in plan.matches]
        params = {"doomed": doomed, "survivors": survivors}

        moved_watches = (await db.execute(_EPISODE_WRITES.watch, params)).all()
        moved_ratings = (await db.execute(_EPISODE_WRITES.rating, params)).all()
        moved_activity = await db.execute(_EPISODE_WRITES.activity, params)
        counts["watches_moved"] = len(moved_watches)
        counts["ratings_moved"] = len(moved_ratings)
        counts["activity_moved"] = moved_activity.rowcount  # type: ignore[attr-defined]

        # §4.3 — history that landed in a show the user does not track is intact
        # by row count and invisible in the product. The destination is read off
        # the rows the UPDATEs actually moved (`RETURNING`), not inferred from
        # the plan, so nobody gets a show added on the strength of a move that
        # a collision withheld.
        destinations = {
            m.twin_id: m.twin_show_id for m in plan.matches if m.twin_show_id != plan.show_id
        }
        tracking = {
            (row.user_id, destinations[row.episode_id])
            for row in (*moved_watches, *moved_ratings)
            if row.episode_id in destinations
        }
        if tracking:
            created = await db.execute(
                pg_insert(UserShowWatch)
                .values([{"user_id": user_id, "show_id": show_id} for user_id, show_id in tracking])
                .on_conflict_do_nothing(index_elements=["user_id", "show_id"])
            )
            counts["show_watches_created"] = created.rowcount  # type: ignore[attr-defined]

    params = {"orphans": orphan_ids}
    counts["watches_deleted"] = (await db.execute(_DELETE_EPISODE_WATCHES, params)).rowcount  # type: ignore[attr-defined]
    counts["ratings_deleted"] = (await db.execute(_DELETE_EPISODE_RATINGS, params)).rowcount  # type: ignore[attr-defined]
    counts["activity_deleted"] = (await db.execute(_DELETE_EPISODE_EVENTS, params)).rowcount  # type: ignore[attr-defined]

    deleted = await db.execute(_DELETE_EPISODES, params)
    if deleted.rowcount != len(orphan_ids):  # type: ignore[attr-defined]
        await db.rollback()
        raise OrphanRetireAborted(
            f"show {plan.show_id}: selected {len(orphan_ids)} orphan episode(s) but "
            f"deleted {deleted.rowcount}; a user row still points at one of them "  # type: ignore[attr-defined]
            f"or an ingested row reached the work list — refusing to continue"
        )
    counts["episodes_deleted"] = deleted.rowcount  # type: ignore[attr-defined]
    await db.commit()
    return counts


async def _retire_seasons(db: AsyncSession, *, batch_size: int) -> dict[str, int]:
    """Delete every orphan season, re-pointing anything still hanging off it first.

    Two production seasons hold **ingested** episodes, so this cannot be a bare
    `DELETE` — `episode.season_id` is `ON DELETE SET NULL`, and a TMDB-sourced
    episode silently losing its season is the read-path regression `season_dedupe`
    already learned to avoid. Where no ingested season carries the number, the
    episode does end up with a null `season_id`; that is counted and reported
    rather than hidden, and the read paths key on `(show_id, season_number)`,
    which the episode still carries.
    """
    doomed = [row.id for row in (await db.execute(_ORPHAN_SEASONS)).all()]
    repointed = orphaned = deleted = 0

    for start in range(0, len(doomed), batch_size):
        batch = doomed[start : start + batch_size]
        params = {"doomed": batch}
        repointed += (await db.execute(_REPOINT_SEASON_EPISODES, params)).rowcount  # type: ignore[attr-defined]
        orphaned += (await db.execute(_EPISODES_ON_SEASONS, params)).scalar_one()
        gone = await db.execute(_DELETE_SEASONS, params)
        if gone.rowcount != len(batch):  # type: ignore[attr-defined]
            await db.rollback()
            raise OrphanRetireAborted(
                f"selected {len(batch)} orphan season(s) but deleted "
                f"{gone.rowcount}; refusing to continue"  # type: ignore[attr-defined]
            )
        deleted += gone.rowcount  # type: ignore[attr-defined]
        await db.commit()

    return {
        "seasons_deleted": deleted,
        "episodes_repointed_to_ingested_season": repointed,
        "episodes_left_without_season": orphaned,
    }


async def _blocked_by_import_ne(db: AsyncSession, shows: Sequence[int]) -> set[int]:
    """Orphan shows an `import_ne` staging row still references, so cannot be deleted."""
    if not shows or (await db.execute(_IMPORT_NE_TABLE)).scalar_one() is None:
        return set()
    rows = await db.execute(_IMPORT_NE_REFERENCES, {"shows": list(shows)})
    return set(rows.scalars())


async def _retire_shows(
    db: AsyncSession, links: dict[int, ShowLink]
) -> tuple[dict[str, int], list[int]]:
    """Move an orphan show's user rows onto its linked counterpart, then delete it.

    Both production orphan shows are tracked by a user and both have a
    counterpart, so the link is what stops this being a straight loss: Cunk on
    Earth's history moves to the anthology TMDB models it as a season of, and
    Discretion's to its sole same-named sibling. An orphan show with no link
    keeps nothing — its user rows are deleted with it, and they are on the
    report's loss list.
    """
    orphan_shows = [row.id for row in (await db.execute(_ORPHAN_SHOWS)).all()]
    counts: dict[str, int] = dict.fromkeys(
        (
            "show_watches_moved",
            "show_ratings_moved",
            "show_activity_moved",
            "show_watches_deleted",
            "show_ratings_deleted",
            "show_activity_deleted",
            "shows_deleted",
        ),
        0,
    )
    if not orphan_shows:
        return counts, []

    linked = [(s, links[s].to_show_id) for s in orphan_shows if s in links]
    if linked:
        params = {"doomed": [s for s, _ in linked], "survivors": [t for _, t in linked]}
        counts["show_watches_moved"] = (await db.execute(_SHOW_WRITES.watch, params)).rowcount  # type: ignore[attr-defined]
        counts["show_ratings_moved"] = (await db.execute(_SHOW_WRITES.rating, params)).rowcount  # type: ignore[attr-defined]
        counts["show_activity_moved"] = (await db.execute(_SHOW_WRITES.activity, params)).rowcount  # type: ignore[attr-defined]

    # A show still holding catalog children, or referenced by the Next Episode
    # staging tables, is left standing rather than cascaded over. Both are
    # reported; neither is silently worked around.
    blocked = {row.id for row in (await db.execute(_SHOWS_WITH_CHILDREN, {"shows": orphan_shows}))}
    blocked |= await _blocked_by_import_ne(db, orphan_shows)
    deletable = [s for s in orphan_shows if s not in blocked]

    if deletable:
        params = {"shows": deletable}
        counts["show_watches_deleted"] = (await db.execute(_DELETE_SHOW_WATCHES, params)).rowcount  # type: ignore[attr-defined]
        counts["show_ratings_deleted"] = (await db.execute(_DELETE_SHOW_RATINGS, params)).rowcount  # type: ignore[attr-defined]
        counts["show_activity_deleted"] = (await db.execute(_DELETE_SHOW_EVENTS, params)).rowcount  # type: ignore[attr-defined]
        gone = await db.execute(_DELETE_SHOWS, params)
        if gone.rowcount != len(deletable):  # type: ignore[attr-defined]
            await db.rollback()
            raise OrphanRetireAborted(
                f"selected {len(deletable)} orphan show(s) but deleted "
                f"{gone.rowcount}; refusing to continue"  # type: ignore[attr-defined]
            )
        counts["shows_deleted"] = gone.rowcount  # type: ignore[attr-defined]
    await db.commit()
    return counts, sorted(blocked)


def _link_row(link: ShowLink, *, episodes: int, user_touched: int | None = None) -> dict[str, Any]:
    return {
        "from_show_id": link.from_show_id,
        "to_show_id": link.to_show_id,
        "season_offset": link.season_offset,
        "evidence": link.evidence,
        "basis": link.basis,
        "episodes_moved": episodes,
        "user_touched": user_touched,
    }


async def retire_orphans(
    db: AsyncSession,
    *,
    limit: int | None = None,
    min_ingested: int = MIN_INGESTED_SHOWS,
    season_batch_size: int = SEASON_BATCH_SIZE,
) -> RetireResult:
    """Retire every orphan row at all three grains: episodes, then seasons, then shows.

    `limit` stops the run once it has retired at least that many **orphan
    episodes**, which is how to try a hundred before spending the whole pass. It
    rounds up to a show boundary rather than cutting one short: a show is one
    transaction and one link resolution, and half-retiring it would leave a
    grain nothing downstream expects. Under a limit the season and show phases
    are skipped entirely — a season is only deletable once its episodes are
    gone, and a partial episode pass has not established that for any show it
    never reached.

    Idempotent and resumable: a row leaves the work list by being re-pointed or
    deleted, so a re-run costs only what is genuinely still there. Each show is
    its own transaction, so a failure costs one show.
    """
    await _assert_ingest_ran(db, min_ingested)

    # `_assert_ingest_ran` has already refused below the floor, so the
    # last-resort link route's premise — few orphan shows, many ingested ones —
    # holds by construction here.
    catalog = _Catalog(db, await _ingested_shows_by_folded_name(db), ingest_ran=True)
    shows = await _work_shows(db)

    totals: Counter[str] = Counter()
    links: dict[int, ShowLink] = {}
    link_rows: list[dict[str, Any]] = []
    planned = consumed = 0

    for show in shows:
        if limit is not None and consumed >= limit:
            break
        plan = await build_show_plan(db, show, catalog)
        if plan.link is not None:
            links[show.id] = plan.link
            moved = sum(1 for m in plan.matches if m.twin_show_id != show.id)
            if moved:
                link_rows.append(_link_row(plan.link, episodes=moved))
        if not plan.orphan_count:
            continue
        totals.update(await _apply_episode_plan(db, plan))
        planned += 1
        consumed += plan.orphan_count
        if planned % _LOG_EVERY == 0:
            log.info(
                "%d show(s) planned, %d orphan episode(s) retired so far",
                planned,
                totals["episodes_deleted"],
            )

    if limit is not None:
        log.info("--limit given: skipping the season and show phases, which need a full pass")
        season_counts: dict[str, int] = {
            "seasons_deleted": 0,
            "episodes_repointed_to_ingested_season": 0,
            "episodes_left_without_season": 0,
        }
        show_counts: dict[str, int] = dict.fromkeys(
            (
                "show_watches_moved",
                "show_ratings_moved",
                "show_activity_moved",
                "show_watches_deleted",
                "show_ratings_deleted",
                "show_activity_deleted",
                "shows_deleted",
            ),
            0,
        )
        blocked: list[int] = []
    else:
        season_counts = await _retire_seasons(db, batch_size=season_batch_size)
        show_counts, blocked = await _retire_shows(db, links)

    return RetireResult(
        shows_planned=planned,
        episodes_deleted=totals["episodes_deleted"],
        watches_moved=totals["watches_moved"],
        ratings_moved=totals["ratings_moved"],
        activity_moved=totals["activity_moved"],
        watches_deleted=totals["watches_deleted"],
        ratings_deleted=totals["ratings_deleted"],
        activity_deleted=totals["activity_deleted"],
        show_watches_created=totals["show_watches_created"],
        shows_kept_referenced=tuple(blocked),
        links_used=tuple(link_rows),
        **season_counts,
        **show_counts,
    )


# --------------------------------------------------------------------------
# The report. Read it before spending the pass — it is the artifact §5 asks to
# be reviewed and committed, and the only chance to see the loss list before it
# becomes a loss.
# --------------------------------------------------------------------------

_USER_ROWS_ON_EPISODES = text("""
    SELECT w.user_id, w.episode_id, 'watch' AS kind
      FROM app.user_episode_watch w
     WHERE w.episode_id = ANY(cast(:orphans AS bigint[]))
     UNION ALL
    SELECT r.user_id, r.episode_id, 'rating'
      FROM app.user_episode_rating r
     WHERE r.episode_id = ANY(cast(:orphans AS bigint[]))
""")

_WATCHES_ON_INGESTED = text("""
    SELECT w.user_id, w.episode_id
      FROM app.user_episode_watch w
      JOIN catalog.episode e ON e.id = w.episode_id
     WHERE e.show_id = :show_id AND e.tmdb_id IS NOT NULL
""")

_TRACKED_SHOWS = text(
    "SELECT user_id, show_id FROM app.user_show_watch WHERE show_id = ANY(cast(:shows AS bigint[]))"
)

_USER_ROWS_ON_SHOWS = text("""
    SELECT w.user_id, w.show_id, 'watch' AS kind
      FROM app.user_show_watch w
     WHERE w.show_id = ANY(cast(:shows AS bigint[]))
     UNION ALL
    SELECT r.user_id, r.show_id, 'rating'
      FROM app.user_show_rating r
     WHERE r.show_id = ANY(cast(:shows AS bigint[]))
""")


def collapse_target(orphan: EpisodeRow, ingested: Sequence[EpisodeRow]) -> EpisodeRow | None:
    """The ingested episode this orphan's position collapses into, if any.

    The last ingested episode at or before the orphan's number, within its own
    season. **Deliberately not the adjacent number**: the probe that produced
    §6's first loss list asked whether the *next* ingested number was watched,
    which classified Friends `s6e24 "Part 1"` as a de-duplication and its
    `s6e25 "Part 2"` twin as a loss purely because TMDB merged that pair the
    other way round from the one before it. Asking which row absorbed this
    position answers the same question without depending on which half of a
    two-parter TMDB kept.
    """
    candidates = [
        e
        for e in ingested
        if e.season_number == orphan.season_number and e.episode_number <= orphan.episode_number
    ]
    return max(candidates, key=lambda e: e.episode_number) if candidates else None


@dataclass(frozen=True)
class RetireReport:
    """What the pass would do, in the shape §5 requires it to be reviewable in."""

    orphan_episodes: int
    orphan_seasons: int
    orphan_shows: int
    ingested_shows: int
    by_cause: dict[str, int]
    by_tier: dict[str, int]
    by_tier_user_touched: dict[str, int]
    to_delete: int
    to_delete_user_touched: int
    rejections: dict[str, int]
    links: tuple[dict[str, Any], ...]
    links_dropped_multiple_candidates: int
    show_watches_to_create: int
    watch_rows_to_move: int
    rating_rows_to_move: int
    watch_rows_to_delete: int
    rating_rows_to_delete: int
    losses: tuple[dict[str, Any], ...]
    loss_summary: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "orphan_episodes": self.orphan_episodes,
            "orphan_seasons": self.orphan_seasons,
            "orphan_shows": self.orphan_shows,
            "ingested_shows": self.ingested_shows,
            "by_cause": dict(self.by_cause),
            "by_tier": dict(self.by_tier),
            "by_tier_user_touched": dict(self.by_tier_user_touched),
            "to_delete": self.to_delete,
            "to_delete_user_touched": self.to_delete_user_touched,
            "rejections": dict(self.rejections),
            "links": [dict(link) for link in self.links],
            "links_dropped_multiple_candidates": self.links_dropped_multiple_candidates,
            "show_watches_to_create": self.show_watches_to_create,
            "watch_rows_to_move": self.watch_rows_to_move,
            "rating_rows_to_move": self.rating_rows_to_move,
            "watch_rows_to_delete": self.watch_rows_to_delete,
            "rating_rows_to_delete": self.rating_rows_to_delete,
            "losses": [dict(loss) for loss in self.losses],
            "loss_summary": dict(self.loss_summary),
        }


async def build_report(db: AsyncSession, *, min_ingested: int = MIN_INGESTED_SHOWS) -> RetireReport:
    """Walk every show through the same planner the pass uses and tally what it decided.

    Writes nothing and needs no TMDB credential, so it is safe against
    production — and it runs the *identical* matcher, so the tier counts and the
    loss list it prints are what the pass will actually do rather than a second
    query maintained alongside it.

    `min_ingested` is **not** a floor here: the report deliberately runs below
    one, because it is the half you run to decide. It only gates the last-resort
    link route, whose cost is bounded by the ingest having happened — see
    `_Catalog.may_search_titles`.
    """
    counts = (await db.execute(_COUNT_ORPHANS)).one()
    ingested_shows = await ingested_show_count(db)
    catalog = _Catalog(
        db,
        await _ingested_shows_by_folded_name(db),
        ingest_ran=ingested_shows >= min_ingested,
    )
    shows = await _work_shows(db)

    by_cause: Counter[str] = Counter()
    by_tier: Counter[str] = Counter()
    by_tier_touched: Counter[str] = Counter()
    rejections: Counter[str] = Counter()
    links: list[dict[str, Any]] = []
    losses: list[dict[str, Any]] = []
    loss_summary: Counter[str] = Counter()
    to_delete = to_delete_touched = dropped_links = 0
    watches_move = ratings_move = watches_delete = ratings_delete = 0
    tracking_wanted: set[tuple[Any, int]] = set()
    show_links: dict[int, ShowLink] = {}

    for show in shows:
        plan = await build_show_plan(db, show, catalog)
        by_cause.update(plan.causes)
        rejections.update(plan.rejections)
        if plan.link is not None:
            show_links[show.id] = plan.link
        if plan.link is None and plan.link_candidates > 1:
            dropped_links += 1
        if not plan.orphan_count:
            # An orphan show can carry a link and no episodes at all —
            # Discretion, one of the three links the acceptance criteria name by
            # id. Its link still moves that show's My Shows and rating rows, so
            # it has to reach the artifact before this `continue` skips the
            # episode-grain work there is none of.
            if plan.link is not None and plan.show_tmdb_id is None:
                links.append(_link_row(plan.link, episodes=0, user_touched=0))
            continue

        orphan_ids = [m.orphan_id for m in plan.matches] + list(plan.deletions)
        user_rows = (await db.execute(_USER_ROWS_ON_EPISODES, {"orphans": orphan_ids})).all()
        touched_by_episode: dict[int, list[Any]] = defaultdict(list)
        for row in user_rows:
            touched_by_episode[row.episode_id].append(row)

        # What the twins already carry decides move-versus-drop: a user holding
        # rows on both sides keeps the twin's, and the orphan's is deleted as
        # redundant (§4.2). Reported as a de-duplication rather than silently
        # counted as a move that did not happen.
        twin_rows: set[tuple[Any, int, str]] = set()
        if plan.matches:
            twin_rows = {
                (row.user_id, row.episode_id, row.kind)
                for row in (
                    await db.execute(
                        _USER_ROWS_ON_EPISODES,
                        {"orphans": [m.twin_id for m in plan.matches]},
                    )
                ).all()
            }

        for match in plan.matches:
            by_tier[match.tier] += 1
            rows = touched_by_episode.get(match.orphan_id, ())
            if rows:
                by_tier_touched[match.tier] += 1
            for row in rows:
                if (row.user_id, match.twin_id, row.kind) in twin_rows:
                    if row.kind == "watch":
                        watches_delete += 1
                    else:
                        ratings_delete += 1
                    loss_summary[LOSS_DEDUPLICATION] += 1
                    orphan = plan.orphans_by_id[match.orphan_id]
                    losses.append(
                        {
                            "disposition": LOSS_DEDUPLICATION,
                            "user_id": str(row.user_id),
                            "show_id": show.id,
                            "show_name": show.name,
                            "season_number": orphan.season_number,
                            "episode_number": orphan.episode_number,
                            "episode_name": orphan.name,
                            "air_date": orphan.air_date.isoformat() if orphan.air_date else None,
                            "row_kind": row.kind,
                            "absorbed_by_episode_id": match.twin_id,
                        }
                    )
                    continue
                if row.kind == "watch":
                    watches_move += 1
                else:
                    ratings_move += 1
                if match.twin_show_id != show.id:
                    tracking_wanted.add((row.user_id, match.twin_show_id))

        if plan.link is not None:
            moved = [m for m in plan.matches if m.twin_show_id != show.id]
            if moved or plan.show_tmdb_id is None:
                links.append(
                    _link_row(
                        plan.link,
                        episodes=len(moved),
                        user_touched=sum(1 for m in moved if m.orphan_id in touched_by_episode),
                    )
                )

        to_delete += len(plan.deletions)
        doomed_touched = [o for o in plan.deletions if o in touched_by_episode]
        to_delete_touched += len(doomed_touched)

        if doomed_touched:
            # Only shows that would actually lose something pay for this pair of
            # queries — five users across the whole production catalog.
            ingested = list(plan.ingested_by_id.values())
            watched_ingested = {
                (row.user_id, row.episode_id)
                for row in (await db.execute(_WATCHES_ON_INGESTED, {"show_id": show.id})).all()
            }
            for orphan_id in doomed_touched:
                orphan = plan.orphans_by_id[orphan_id]
                target = collapse_target(orphan, ingested)
                for row in touched_by_episode[orphan_id]:
                    if row.kind == "watch":
                        watches_delete += 1
                    else:
                        ratings_delete += 1
                    absorbed = target is not None and (row.user_id, target.id) in watched_ingested
                    disposition = LOSS_DEDUPLICATION if absorbed else LOSS_GENUINE
                    loss_summary[disposition] += 1
                    losses.append(
                        {
                            "disposition": disposition,
                            "user_id": str(row.user_id),
                            "show_id": show.id,
                            "show_name": show.name,
                            "season_number": orphan.season_number,
                            "episode_number": orphan.episode_number,
                            "episode_name": orphan.name,
                            "air_date": orphan.air_date.isoformat() if orphan.air_date else None,
                            "row_kind": row.kind,
                            "absorbed_by_episode_id": (
                                target.id if absorbed and target is not None else None
                            ),
                        }
                    )

    # §2.5a: a wrong link among the 129 carrying no user rows costs nothing,
    # because tier 2 and tier 3 produce the same end state for an untouched
    # orphan — so `user_touched` on each row is what makes the review gate
    # targeted rather than 130 links to read by hand. Worst first.
    links.sort(key=lambda row: (-row["user_touched"], -row["episodes_moved"]))

    tracked: set[tuple[Any, int]] = set()
    if tracking_wanted:
        rows = await db.execute(
            _TRACKED_SHOWS, {"shows": sorted({show_id for _, show_id in tracking_wanted})}
        )
        tracked = {(row.user_id, row.show_id) for row in rows}

    # Show-grain rows on an orphan show. **Two** ways one is deleted rather than
    # moved, and both have to reach the loss list or criterion 4's "no unlisted
    # LOST line" holds only at episode grain: the show has no link at all (a
    # genuine loss), or it has one but the user already holds the same row on the
    # destination — which `_SHOW_WRITES`'s `NOT EXISTS` withholds and
    # `_DELETE_SHOW_WATCHES` then removes (a de-duplication, §4.2 one grain up).
    orphan_show_ids = [row.id for row in (await db.execute(_ORPHAN_SHOWS)).all()]
    if orphan_show_ids:
        destinations = sorted(
            {
                link.to_show_id
                for show_id, link in show_links.items()
                if show_id in set(orphan_show_ids)
            }
        )
        on_destinations: set[tuple[Any, int, str]] = set()
        if destinations:
            on_destinations = {
                (row.user_id, row.show_id, row.kind)
                for row in (await db.execute(_USER_ROWS_ON_SHOWS, {"shows": destinations})).all()
            }
        for row in (await db.execute(_USER_ROWS_ON_SHOWS, {"shows": orphan_show_ids})).all():
            link = show_links.get(row.show_id)
            if link is not None and (row.user_id, link.to_show_id, row.kind) not in on_destinations:
                continue
            disposition = LOSS_GENUINE if link is None else LOSS_DEDUPLICATION
            loss_summary[disposition] += 1
            losses.append(
                {
                    "disposition": disposition,
                    "user_id": str(row.user_id),
                    "show_id": row.show_id,
                    "show_name": None,
                    "season_number": None,
                    "episode_number": None,
                    "episode_name": None,
                    "air_date": None,
                    "row_kind": f"show_{row.kind}",
                    "absorbed_by_episode_id": None,
                }
            )

    return RetireReport(
        orphan_episodes=counts.episodes,
        orphan_seasons=counts.seasons,
        orphan_shows=counts.shows,
        ingested_shows=ingested_shows,
        by_cause=dict(by_cause),
        by_tier={tier: by_tier[tier] for tier in TIERS},
        by_tier_user_touched={tier: by_tier_touched[tier] for tier in TIERS},
        to_delete=to_delete,
        to_delete_user_touched=to_delete_touched,
        rejections=dict(rejections),
        links=tuple(links),
        links_dropped_multiple_candidates=dropped_links,
        show_watches_to_create=len(tracking_wanted - tracked),
        watch_rows_to_move=watches_move,
        rating_rows_to_move=ratings_move,
        watch_rows_to_delete=watches_delete,
        rating_rows_to_delete=ratings_delete,
        losses=tuple(losses),
        loss_summary=dict(loss_summary),
    )
