"""The JSON a push carries — the payload contract shared with `sw.js` (spec §5.3).

Pure over a `Candidate`: every field the worker shows is rendered here, because
the worker never fetches. `url` is SPA-relative (the worker prefixes its
origin) and the worker sets the notification `tag` from `key`, so a re-send of
the same notification replaces it rather than stacking.

**Under 4 KB, by construction.** Web Push caps the encrypted record at 4096
bytes, and `sender.send` serialises with `json.dumps`' default ASCII escaping,
where one astral-plane character costs twelve bytes. The two free-text fields
are therefore clipped to `MAX_TEXT_CHARS`, which keeps the worst case (every
character an emoji) under the limit; everything else is short and bounded.
"""

from collections.abc import Callable
from datetime import date

from tvbf.catalog.images import POSTER, image_url
from tvbf.push.candidates import AiredEpisode, Candidate

APP_NAME = "TV BingeFriend"
MAX_TEXT_CHARS = 120
# An episode title is clipped inside the airs-today body rather than the body
# as a whole, so a long one still reads "… airs today".
_EPISODE_TITLE_CHARS = 80
_ICON_SIZE = "w185"
_ELLIPSIS = "…"
_EN_DASH = "–"


def _clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(_ELLIPSIS)] + _ELLIPSIS


def _mon_d(value: date | None) -> str:
    """`Oct 5` — no leading zero, no year: a premiere worth announcing is soon."""
    if value is None:
        raise ValueError("a premiere candidate carries its season's corrected air_date")
    return f"{value:%b} {value.day}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _list_that_fits(items: tuple[str, ...], limit: int, more: Callable[[int], str]) -> str:
    """`a, b and 2 more …` — as many whole items as fit `limit`.

    Whole items rather than a clipped string, so the list never ends mid-item;
    only a first item too long to fit on its own is clipped. `more(rest)` is
    the tail for `rest` items left out, and must be empty for zero.
    """
    if not items:
        return ""
    for shown in range(len(items), 0, -1):
        text = ", ".join(items[:shown]) + more(len(items) - shown)
        if len(text) <= limit:
            return text
    suffix = more(len(items) - 1)
    return _clip(items[0], limit - len(suffix)) + suffix


def _show_list(names: tuple[str, ...], limit: int = MAX_TEXT_CHARS) -> str:
    """`Andor, Severance and 2 more shows`."""
    return _list_that_fits(
        names, limit, lambda rest: f" and {_plural(rest, 'more show')}" if rest else ""
    )


def _episode_code(episode: AiredEpisode) -> str:
    return f"S{episode.season_number}E{episode.episode_number}"


def _episode_codes(episodes: tuple[AiredEpisode, ...], limit: int) -> str:
    """`S2E1–E8` for one season's unbroken run — the season-dump shape — else
    `S2E1, S2E3, S3E1`, as many whole codes as fit and then `and 4 more`."""
    first, last = episodes[0], episodes[-1]
    numbers = [episode.episode_number for episode in episodes]
    one_season = all(episode.season_number == first.season_number for episode in episodes)
    if one_season and numbers == list(
        range(first.episode_number, first.episode_number + len(numbers))
    ):
        return f"{_episode_code(first)}{_EN_DASH}E{last.episode_number}"
    return _list_that_fits(
        tuple(_episode_code(episode) for episode in episodes),
        limit,
        lambda rest: f" and {rest} more" if rest else "",
    )


def _airs_today_body(candidate: Candidate) -> str:
    if len(candidate.episodes) > 1:
        # The count is the news; the codes are the detail, and titles do not
        # fit. The codes get whatever room the fixed words leave (NEU-1539).
        head = f"{_plural(len(candidate.episodes), 'episode')} air today ("
        codes = _episode_codes(candidate.episodes, MAX_TEXT_CHARS - len(head) - 1)
        return f"{head}{codes})"
    code = f"S{candidate.season_number}E{candidate.episode_number}"
    if candidate.episode_name:
        return f"{code} “{_clip(candidate.episode_name, _EPISODE_TITLE_CHARS)}” airs today"
    return f"{code} airs today"


def _body(candidate: Candidate) -> str:
    match candidate.kind:
        case "airs_today":
            return _airs_today_body(candidate)
        case "premiere_set":
            return f"Season {candidate.season_number} premieres {_mon_d(candidate.air_date)}"
        case "premiere_moved":
            return f"Season {candidate.season_number} moved to {_mon_d(candidate.air_date)}"
        case "ended":
            if candidate.status == "Canceled":
                return "Marked as cancelled"
            return "Marked as ended"
        case "revived":
            return "Renewed — more episodes are coming"
        case "summary":
            return _show_list(candidate.show_names)


def _url(candidate: Candidate) -> str:
    if candidate.kind == "summary":
        return "/upcoming"
    if candidate.kind == "airs_today":
        if len(candidate.episodes) > 1:
            # The season's episode list, where every one of them is (NEU-1539).
            season = candidate.episodes[0].season_number
            return f"/shows/{candidate.show_id}/episodes?season={season}"
        return f"/episodes/{candidate.episode_id}"
    return f"/shows/{candidate.show_id}"


def _summary_title(candidate: Candidate) -> str:
    """By delivery task (NEU-1540): `2 more shows air today` for the airs-today
    task's overflow, `2 more updates today` for the events task's."""
    count = candidate.count or 0
    if candidate.task == "airs_today":
        verb = "airs" if count == 1 else "air"
        return f"{_plural(count, 'more show')} {verb} today"
    return f"{_plural(count, 'more update')} today"


def build_payload(candidate: Candidate) -> dict[str, str]:
    """The §5.3 JSON for one notification. `icon` is omitted when there is no poster."""
    if candidate.kind == "summary":
        # Not the app name: iOS already prints "from TV BingeFriend" under the title.
        title = _summary_title(candidate)
    else:
        title = candidate.show_name or APP_NAME
    payload = {
        "key": candidate.key,
        "kind": candidate.kind,
        "title": _clip(title),
        "body": _clip(_body(candidate)),
        "url": _url(candidate),
    }
    icon = image_url(candidate.poster_path, POSTER, _ICON_SIZE)
    if icon is not None:
        payload["icon"] = icon
    return payload
