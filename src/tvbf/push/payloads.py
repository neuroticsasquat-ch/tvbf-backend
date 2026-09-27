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

from datetime import date

from tvbf.catalog.images import POSTER, image_url
from tvbf.push.candidates import Candidate

APP_NAME = "TV BingeFriend"
MAX_TEXT_CHARS = 120
# An episode title is clipped inside the airs-today body rather than the body
# as a whole, so a long one still reads "… airs today".
_EPISODE_TITLE_CHARS = 80
_ICON_SIZE = "w185"
_ELLIPSIS = "…"


def _clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(_ELLIPSIS)] + _ELLIPSIS


def _mon_d(value: date | None) -> str:
    """`Oct 5` — no leading zero, no year: a premiere worth announcing is soon."""
    if value is None:
        raise ValueError("a premiere candidate carries its season's corrected air_date")
    return f"{value:%b} {value.day}"


def _body(candidate: Candidate) -> str:
    match candidate.kind:
        case "airs_today":
            code = f"S{candidate.season_number}E{candidate.episode_number}"
            if candidate.episode_name:
                return f"{code} “{_clip(candidate.episode_name, _EPISODE_TITLE_CHARS)}” airs today"
            return f"{code} airs today"
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
            return f"{candidate.count} more updates today"


def _url(candidate: Candidate) -> str:
    if candidate.kind == "summary":
        return "/upcoming"
    if candidate.kind == "airs_today":
        return f"/episodes/{candidate.episode_id}"
    return f"/shows/{candidate.show_id}"


def build_payload(candidate: Candidate) -> dict[str, str]:
    """The §5.3 JSON for one notification. `icon` is omitted when there is no poster."""
    title = APP_NAME if candidate.kind == "summary" else (candidate.show_name or APP_NAME)
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
