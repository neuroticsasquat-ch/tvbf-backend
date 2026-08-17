"""The instruction the model is given, and how its answer is read back.

Project spec §7. This module is the whole of what the weekly pass says to the
provider and the whole of what it believes back — the request half is
`INSTRUCTION` plus `build_prompt`, the response half is `parse_suggestions` and
`quoted_candidate`. They live together because they are one contract: the output
shape asserted in the instruction and the shape the parser insists on cannot be
edited apart without one of them lying. That is also why the repair for a title
the model *dressed* lives here rather than in `resolution.py`: knowing how this
model breaks §7 is knowledge about the contract, not about the catalog.

## Editing this file means bumping `PROMPT_VERSION`

`payload.PROMPT_VERSION` is *in* the regeneration hash (§9.1), so a prompt
change that leaves it alone changes what the pass would produce while every
stored hash still matches and nobody ever regenerates. Bump it in the same
commit as any change to `INSTRUCTION`, and shipping the change re-runs every
user exactly once — which is also the only way to evaluate a prompt edit
against real users.

## The literal word "json"

`llm/client._to_wire` refuses an instruction that does not contain it, in
lower case, in `system`. That guard is knowingly stricter than the provider
measured on 2026-08-15 (NEU-1100) and the reasoning for keeping it lives at
the check itself. What it means here is that the word below is load-bearing
rather than incidental phrasing: a rewrite that drops it raises before
anything is sent.

## The exclusion rule is stated twice on purpose

The instruction tells the model never to recommend a series in the input, and
the pass filters the resolved ids against `excluded_show_ids` afterwards
(§8, "belt-and-braces"). The instruction alone would be a request; the filter
alone would waste however many of the 25 the model spent echoing the library
back. Neither replaces the other.

## Dropping is the parser's whole error handling

A recommendation missing `release_year` is dropped, because that year is the
only disambiguator resolution has (§7) — the alternative is an unbounded
title match, which is the failure mode §8 refuses a fuzzy fallback to avoid.
Title and `reason` are required on the same terms: a card with no title is
not renderable and a card with no reason is a blank line of body text.

A response that is not the contract *at all* — no `recommendations` key, or
one that is not a list — is `LLMResponseInvalid` instead, so the pass's single
retry covers it. That is the same class the client raises for a body that did
not decode, and deliberately so: both are "a response arrived and could not be
believed", both are one-off often enough to be worth one more call, and the
job dispatches on the taxonomy rather than on where in the stack the verdict
was reached.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tvbf.llm.types import LLMResponseInvalid, Prompt

log = logging.getLogger(__name__)

RECOMMENDATION_COUNT = 25
"""How many suggestions to ask for, against the twelve a surface shows (§7).

The headroom absorbs resolution failures, the never-recommend filter, and the
`adult` / `deleted_upstream_at` filtering that happens at *read* time because a
set generated in March can name a show tombstoned in June.
"""

INSTRUCTION = (
    "You recommend television series to one person, based only on what they have "
    "watched. The user message is a json object describing their viewing: "
    '"columns" names the fields of every row, "liked" are series they enjoyed, '
    '"not_liked" are series they did not, and "interested" are series they have '
    'added but not started. In each row, "pct" is how much of that series they '
    'have watched and "stars" is their rating out of five, or null if they have '
    "not rated it. "
    '"exclude" is a plain list of further series they already have, with the '
    'fields "exclude_columns" names and no viewing data — it is there only so '
    "you can avoid them.\n"
    "Reply with a json object of the form "
    '{"recommendations": [{"title": "...", "release_year": 1234, '
    '"reason": "one sentence"}]} and nothing else. Give exactly '
    f"{RECOMMENDATION_COUNT} recommendations, best first. "
    '"title" is the series title as it is best known in English and nothing '
    "else: no commentary, no comparison with another series, no alternative "
    "suggestion, no parenthetical and no quotation marks. "
    '"release_year" is the year the series first aired, and "reason" is one '
    "plain sentence saying why this person in particular would like it — prose "
    'only, no markup. Every explanation belongs in "reason": a title carrying '
    "one cannot be looked up, and that recommendation is discarded. "
    "Every series named anywhere in the user message is one this person already "
    'has — in "liked", in "not_liked", in "interested" or in "exclude" — so none '
    "of them may appear in your answer. When the series you were about to "
    "recommend is one of them, drop it without comment and give the next best "
    "one you have not used. Do not name it, do not describe it, and do not "
    "offer it with a caveat: an answer that returns this person's own series to "
    "them is discarded whole and they see nothing."
)
"""The instruction that does not vary between users. See the module docstring.

The rows are described rather than left to be inferred because the payload is
columnar to save tokens (§5.3) and a header of four bare names is not
self-explanatory — `pct` in particular reads as a percentage of *what* only
once somebody says so.

**The title clause is blunt because a real answer was not.** On 2026-08-17 a
production run stored 5 of 25 because the model wrote its reasoning into
`title` — `"The Americans' Russian counterpart, 'The Bureau'"`,
`"Succession's corporate peer, 'Industry' (though you've seen it), try
'Billions'"` — while every entry stayed *structurally* valid, so nothing
before resolution could object. Resolution is fold-exact by design (§8 refuses
a fuzzy fallback), so a title carrying prose matches nothing and reads in the
log as a catalog gap. Hence naming the failure modes rather than only asking
for a title, and saying what it costs.

**The exclusion paragraph is the second draft, and the first one made things
worse.** The prose that broke the run above editorialised specifically around
shows the user had already seen — `"Industry (though you've seen it), try
'Billions'"`. Read closely, that was the model *attempting to comply*: naming
its best fit, noticing the user had it, and redirecting. `PROMPT_VERSION` 2
answered with "never say that you are avoiding one — pick a different series
instead", and on 2026-08-17 at 16:00 UTC the model dropped the **redirect**
rather than the seen show: 25 clean bare titles, 25 of 25 already in its own
input, nothing stored, recorded `no_matches`. Removing the narration removed
the mechanism the narration was part of.

So this draft says the same thing in the other direction — it names all four
groups the ban covers, tells the model what to do *instead* ("drop it without
comment and give the next best one you have not used"), and states the
consequence for the person rather than for the parser, because "discarded" is
not a cost the model has any reason to weigh and "they see nothing" is.

**Which is also why `exclude` exists at all** (`payload.EXCLUDE_COLUMNS`). Under
versions 1 and 2 the ban covered shows the payload never mentioned — the
INTERESTED cap's overflow, a show carrying only an episode rating — so the model
was asked to avoid rows it could not see. An instruction that cannot be followed
is not an instruction.
"""


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One recommendation as the model authored it, before resolution.

    Prose and a year, which is all the model has to offer: turning it into a
    `catalog.show` id is `recommendations/resolution.py`'s job and deliberately
    not this module's.
    """

    title: str
    release_year: int
    reason: str


def build_prompt(payload_json: str) -> Prompt:
    """The request for one user: the fixed instruction, and their taste payload.

    The payload arrives as the canonical bytes `payload.build_payload` hashed,
    not as a re-serialization of the same document — the hash promises that
    identical bytes mean identical output, and a second `json.dumps` with
    different separators would quietly break that promise.
    """
    return Prompt(system=INSTRUCTION, user=payload_json)


def parse_suggestions(parsed: Mapping[str, Any]) -> tuple[list[Suggestion], list[Any]]:
    """The usable recommendations in the model's own order, and what was dropped.

    The dropped entries are returned rather than only counted so the caller can
    log them: a systematic shape change upstream reads as "3 of 25 dropped"
    every week, and the entries themselves are what say why. They are also
    preserved verbatim in `raw_response` regardless.
    """
    entries = parsed.get("recommendations")
    if not isinstance(entries, list):
        raise LLMResponseInvalid(
            "the response carried no `recommendations` list "
            f"(keys: {sorted(str(key) for key in parsed)})"
        )

    suggestions: list[Suggestion] = []
    dropped: list[Any] = []
    for entry in entries:
        suggestion = _suggestion(entry)
        if suggestion is None:
            dropped.append(entry)
        else:
            suggestions.append(suggestion)
    return suggestions, dropped


def _suggestion(entry: Any) -> Suggestion | None:
    """One entry, or `None` if it is not the contract §7 asked for."""
    if not isinstance(entry, Mapping):
        return None
    title = _text(entry.get("title"))
    reason = _text(entry.get("reason"))
    year = entry.get("release_year")
    # `bool` is an `int` in Python and `True` would otherwise resolve as the
    # year 1. Nothing sane sends one; the check costs a clause.
    if title is None or reason is None or not isinstance(year, int) or isinstance(year, bool):
        return None
    return Suggestion(title=title, release_year=year, reason=reason)


def _text(value: Any) -> str | None:
    """A non-empty string with its surrounding whitespace removed, or `None`."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def describe_dropped(dropped: Sequence[Any]) -> str:
    """A one-line summary of dropped entries, for the pass's log.

    Titles where there is one, because "3 entries dropped" cannot be acted on
    and "3 entries dropped (Foo, <no title>, Bar)" can.
    """
    return ", ".join(_dropped_label(entry) for entry in dropped)


def _dropped_label(entry: Any) -> str:
    if not isinstance(entry, Mapping):
        return "<not an object>"
    return _text(entry.get("title")) or "<no title>"


_CLOSERS: dict[str, str] = {"'": "'", '"': '"', "’": "‘", "”": "“"}
"""Each delimiter that can *close* a quoted run, mapped to the one that opens it.

Straight quotes partner with themselves. The typographic pairs partner across
their forms, which is what makes them the more reliable of the two: `’` is
ambiguous between a possessive and a close-quote, but `‘` unambiguously opens,
so a possessive `’` finds no partner and yields nothing rather than a guess.
"""

_DELIMITERS = frozenset(_CLOSERS) | frozenset(_CLOSERS.values())


def quoted_candidate(title: str) -> str | None:
    """The show a dressed title is actually recommending, or `None` (NEU-1173).

    The model intermittently answers with a series from the user's own payload,
    a possessive or connective, and then the real recommendation in quotes —
    `"The Leftovers' 'Manhunt: Unabomber'"`. Resolution is fold-exact by design
    (§8), so every such recommendation matches nothing and is lost, and it is
    lost *invisibly*, reading in the log exactly like a catalog gap. Three
    consecutive revisions of `INSTRUCTION` have tried to stop it happening; this
    is the reading side of the same contract, which is why it lives here and not
    in `resolution.py` — the model violated §7, and knowing the shape of its
    violations is what this module is for.

    **The pairing runs from the right.** Every observed dressed title carries an
    *odd* number of apostrophes, because the connective is a possessive, so
    pairing left to right recovers `" sibling "`, `"s "` and `" "` — garbage in
    four cases out of four. Worse, on the version-1 answer recorded in
    `INSTRUCTION`'s docstring it recovers `Industry`, the show the model was
    explicitly declining *because the user already has it*. That asymmetry is
    structural rather than lucky: the leading segment is the series being
    compared against or declined, and the trailing one is the recommendation.

    **One candidate, not many.** Walking every quoted run right to left until one
    sticks is the shape §8 refuses — each extra candidate is another chance for a
    junk segment to land on a real show, and `The Americans` *is* a real show.
    The last run is correct on all five observed cases, at one extra query per
    unresolved title.

    Pure: no database, and no fold. `sql_fold.folded` strips punctuation anyway,
    so `'Bodyguard'` and `Bodyguard` fold identically — this decides segment
    boundaries and nothing else, and a candidate carrying stray punctuation is
    not thereby broken. The caller decides what to do with it, and only after the
    title as written has already failed.
    """
    closer_at = _last_delimiter(title)
    if closer_at is None:
        return None
    opener = _CLOSERS.get(title[closer_at])
    if opener is None:
        # The last delimiter opens rather than closes: nothing to its right, and
        # no partner to its left either.
        return None
    opener_at = title.rfind(opener, 0, closer_at)
    if opener_at < 0:
        return None
    candidate = title[opener_at + 1 : closer_at].strip()
    # The raw title already failed to resolve, so an identical second query buys
    # nothing. Unreachable while the rule above excludes both delimiters from the
    # span; it is the contract callers rely on, not an observed case.
    if not candidate or candidate == title:
        return None
    return candidate


def _last_delimiter(title: str) -> int | None:
    """The index of the rightmost quote character, of any kind."""
    for index in range(len(title) - 1, -1, -1):
        if title[index] in _DELIMITERS:
            return index
    return None
