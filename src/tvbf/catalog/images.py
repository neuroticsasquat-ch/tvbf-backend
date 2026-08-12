"""The one place a TMDB image path becomes a URL, and the one record of which
size each API field name means.

TV Maze stored a **full image URL**; TMDB stores a **path fragment** —
`/abc.jpg` — which is only an image once a base URL and a size are prepended.
Something has to compose the two, and NEU-1063 decided the **backend** does it:
`image_medium` and `image_original` stay full URLs, so the API contract is
byte-for-byte what it was and the SPA needs no work at cutover. The alternative
— shipping the raw path and letting each surface pick its own size — is more
honest, but it is a contract change that would need a matching frontend ticket,
and none exists.

**The cost of composing is that the backend picks which TMDB size each name
means, so the mapping is written down here rather than living inside a format
string.** `KINDS` is that record: every size TMDB actually offers per kind, and
which of them `image_medium` / `image_original` resolve to. `ImageKind` rejects
a `medium` or `original` that upstream does not offer, so the available lists
are load-bearing rather than a comment that can drift.

Both halves of every choice are measured, not inferred from docs:

* Sizes come from `GET /configuration`, read 2026-08-12 — unchanged from the
  2026-08-09 reading in NEU-1063 except that this one also records
  `profile_sizes`, which that one did not.
* The TV Maze dimensions each size is chosen against were read off the pixels of
  real `static.tvmaze.com` images (three per kind, from the local mirror), not
  taken from TV Maze's documentation: poster `medium` is 210x295, episode still
  `medium` is 250x140, person profile `medium` is 210x295 — all three exactly,
  every sample.

**A null image is now normal rather than exceptional.** NEU-1042 deliberately
did not copy TV Maze's image URLs into `poster_path` (a full URL is not a path
fragment), so between the copy and the ingest every show has no image, and a
show TMDB never matches has none permanently. Every function here maps an
absent path to `None` rather than to a URL that would 404, and the SPA already
guards each of these fields (`?? FALLBACK_POSTER`, or a `? :` around the
`<img>`), so a null renders the placeholder it always did.

**The caller is `catalog/schemas.py`**, which composes every `image_medium` /
`image_original` in the API from a path. NEU-1063 owned the decision and the
mechanism; NEU-1047 threw the switch, and made `CharacterRef.image_medium`
permanently null in the process — TMDB models a character as free text, so
`catalog.character` has no image column to compose from.
"""

from dataclasses import dataclass

from tvbf.config import get_settings


@dataclass(frozen=True)
class ImageKind:
    """One TMDB image kind, its available sizes, and the two the API exposes.

    `available` is what `GET /configuration` reports for the kind. It is not
    decoration: `__post_init__` refuses a `medium` or `original` outside it, so
    a typo in a size — which upstream answers with a 404 image rather than an
    error — is a failure at import instead of a broken thumbnail in production.
    """

    name: str
    available: tuple[str, ...]
    medium: str
    original: str = "original"

    def __post_init__(self) -> None:
        for field_name in ("medium", "original"):
            size = getattr(self, field_name)
            if size not in self.available:
                raise ValueError(
                    f"{self.name} {field_name} size {size!r} is not one of TMDB's "
                    f"{self.name} sizes {self.available!r}"
                )


# Show and season posters. `w342` (342x513) is the smallest TMDB poster wider
# than TV Maze's 210x295 medium; `w185` (185x278) is narrower than what the SPA
# renders today and would arrive visibly softer on the cards that fill the home
# tabs.
POSTER = ImageKind(
    name="poster",
    available=("w92", "w154", "w185", "w342", "w500", "w780", "original"),
    medium="w342",
)

# Episode stills. `w300` (300x169 at 16:9) is the smallest still wider than TV
# Maze's 250x140 medium, and the only still size in that neighbourhood at all —
# the next one down, `w185`, is 26% narrower.
STILL = ImageKind(
    name="still",
    available=("w92", "w185", "w300", "original"),
    medium="w300",
)

# Person headshots. TMDB offers nothing between `w185` (185x278) and `h632`
# (~421x632), so neither brackets TV Maze's 210x295 the way the two above do.
# `w185` is 12% narrower than what ships today; `h632` is four times the pixels
# for an avatar in a credit list. The thumbnail wins — and `PersonOut` also
# carries `image_original`, which is what `PersonPage` already prefers for the
# one surface that renders a headshot large.
PROFILE = ImageKind(
    name="profile",
    available=("w45", "w185", "h632", "original"),
    medium="w185",
)

# Backdrops. **No API field exposes one today**, and this ticket deliberately
# does not add one: `backdrop_path` has no TV Maze ancestor, so exposing it is
# an additive contract change with no consumer — the audit (NEU-1031) files it
# under Public Profiles & Sharing, which has not been built. It is recorded here
# anyway because `catalog.show.backdrop_path` and `catalog.image` are already
# populated, so the mapping is a real open question the moment that project
# starts, and answering it costs three lines now against a re-measurement later.
# `w780` is the mid size, matched to a hero image on a phone rather than a
# desktop header.
BACKDROP = ImageKind(
    name="backdrop",
    available=("w300", "w780", "w1280", "original"),
    medium="w780",
)

KINDS: tuple[ImageKind, ...] = (POSTER, STILL, PROFILE, BACKDROP)


def image_url(path: str | None, kind: ImageKind, size: str) -> str | None:
    """Compose one TMDB image URL, or `None` when there is no image.

    `size` is checked against `kind.available` on every call rather than trusted
    from the docstring. TMDB answers an unknown size with a placeholder image
    and a 200, so a wrong size is invisible at every layer below the rendered
    page — and a `kind` argument that only *looked* like it constrained `size`
    would be worse than none at all.
    """
    if path is None or not path.strip():
        return None
    if size not in kind.available:
        raise ValueError(f"{size!r} is not one of TMDB's {kind.name} sizes {kind.available!r}")
    base = get_settings().tmdb_image_base_url.rstrip("/")
    return f"{base}/{size}/{path.strip().lstrip('/')}"


def image_pair(path: str | None, kind: ImageKind) -> tuple[str | None, str | None]:
    """The `(image_medium, image_original)` pair the API exposes for `path`.

    Both are `None` for an absent path — never one and not the other, because a
    caller unpacking straight into a response model would otherwise emit a
    medium thumbnail with no full-size counterpart for a show that has neither.
    """
    return image_url(path, kind, kind.medium), image_url(path, kind, kind.original)


def medium_url(path: str | None, kind: ImageKind) -> str | None:
    """Just the `image_medium` half, for the fields that expose only that one.

    `PersonRef`, `ShowRef` and `CharacterRef` carry `image_medium` with no
    `image_original` beside it. Without this they would each have to name a size
    at the call site, which is the drift `KINDS` exists to prevent.
    """
    return image_url(path, kind, kind.medium)


def poster_urls(path: str | None) -> tuple[str | None, str | None]:
    """`(image_medium, image_original)` for a show or season poster."""
    return image_pair(path, POSTER)


def still_urls(path: str | None) -> tuple[str | None, str | None]:
    """`(image_medium, image_original)` for an episode still."""
    return image_pair(path, STILL)


def profile_urls(path: str | None) -> tuple[str | None, str | None]:
    """`(image_medium, image_original)` for a person headshot."""
    return image_pair(path, PROFILE)
