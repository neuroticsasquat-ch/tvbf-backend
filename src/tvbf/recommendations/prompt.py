"""The instruction the model is given, and how its answer is read back.

Project spec §7. This module is the whole of what the weekly pass says to the
provider and the whole of what it believes back — the request half is
`INSTRUCTION` plus `build_prompt`, the response half is `parse_suggestions`.
They live together because they are one contract: the output shape asserted in
the instruction and the shape the parser insists on cannot be edited apart
without one of them lying.

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
    "not rated it.\n"
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
    "Never recommend a series that appears anywhere in the user message, and "
    "never say that you are avoiding one — pick a different series instead."
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

**The last sentence exists because the exclusion rule provoked the first.**
That answer editorialised specifically around shows the user had already seen
("though you've seen it", "but since seen"): the model wanted to explain what
it was skipping and had nowhere to put it at the point of naming. Telling it
not to mention the avoidance is cheaper than a field for reasoning nobody
reads.
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
