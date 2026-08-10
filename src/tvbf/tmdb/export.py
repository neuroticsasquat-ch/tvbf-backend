"""TMDB's daily id export — the authoritative list of every TV series.

This is what `/updates/shows` was for TV Maze, and it is a **static file rather
than an endpoint**:

    https://files.tmdb.org/p/exports/tv_series_ids_MM_DD_YYYY.json.gz

One JSON object per line (`{"id": …, "original_name": …, "popularity": …}`),
228,611 of them as of 2026-08-07, 4.7 MB gzipped. Regenerated daily by 08:00 UTC
and retained three months.

Three things follow from it being a file, and each one is why this module is not
a method on `TMDBClient`.

**It costs no rate budget.** `files.tmdb.org` is a static host, not the API, so a
request here must not spend a token from the shared TMDB bucket — the whole
catalog list arrives for one download that the ingest's ~3.2-hour estimate does
not have to account for.

**It carries no credential.** The file is public. Sending the bearer token to a
host that does not need it widens where that token can leak for no gain, so this
module builds its own client rather than borrowing the authenticated one.

**Today's file may not exist yet.** "By 08:00 UTC" is a deadline, not a
guarantee, and a run triggered at 07:00 would otherwise fail on a 403 from a key
that simply has not been written. `fetch_series_ids` walks backwards a day at a
time; a day-old catalog list costs at most a day's new series, which the delta
picks up anyway.
"""

import gzip
import json
import logging
from datetime import UTC, date, datetime, timedelta

import httpx

log = logging.getLogger(__name__)

EXPORT_BASE_URL = "https://files.tmdb.org/p/exports"

# How many days back to walk before giving up. Two covers the ordinary case —
# a run that starts before the day's file is published — with a day in hand for
# a late regeneration. Beyond that the export is genuinely broken and a run that
# quietly used week-old data would be worse than one that failed.
_MAX_LOOKBACK_DAYS = 2

# S3 answers a key that does not exist with 403 rather than 404 unless listing
# is public, so both mean "not published (yet)" here. Anything else — a 5xx, a
# redirect loop — is a real failure and propagates.
_NOT_PUBLISHED = (403, 404)


def export_url(day: date, *, base_url: str = EXPORT_BASE_URL) -> str:
    """The URL of one day's TV series export.

    The date format is TMDB's: `MM_DD_YYYY`, zero-padded.
    """
    return f"{base_url.rstrip('/')}/tv_series_ids_{day:%m_%d_%Y}.json.gz"


def parse_series_ids(raw: bytes) -> list[int]:
    """Every `id` in one gzipped JSONL export, in file order.

    A line that will not parse is skipped rather than fatal: this is 228k lines
    of somebody else's file, and losing one series to the delta is a better
    outcome than losing a three-hour pass. The count is logged so a file that is
    broadly malformed does not pass for a healthy one.

    An export that yields **no** ids raises. Nothing downstream distinguishes an
    empty work list from a complete catalog, so a truncated or wrong-shaped file
    would otherwise read as "everything is already ingested" and finalise the run
    as a success.
    """
    ids: list[int] = []
    skipped = 0
    for line in gzip.decompress(raw).splitlines():
        if not line.strip():
            continue
        try:
            ids.append(int(json.loads(line)["id"]))
        except (ValueError, KeyError, TypeError):
            skipped += 1
    if skipped:
        log.warning("id export: skipped %d unparseable line(s)", skipped)
    if not ids:
        raise ValueError("the TMDB id export parsed to zero series ids")
    return ids


async def fetch_series_ids(
    *,
    base_url: str = EXPORT_BASE_URL,
    today: date | None = None,
    lookback_days: int = _MAX_LOOKBACK_DAYS,
    timeout: float = 120.0,
) -> list[int]:
    """Download the most recent published export and return its series ids.

    Walks back from `today` (UTC) until a day's file exists. Raises if none of
    the candidates is published, which is a loud failure on purpose — see
    `parse_series_ids` for why a silent empty list is the dangerous outcome.
    """
    start = today or datetime.now(UTC).date()
    attempted: list[str] = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for age in range(lookback_days + 1):
            day = start - timedelta(days=age)
            url = export_url(day, base_url=base_url)
            attempted.append(url)
            resp = await client.get(url)
            if resp.status_code in _NOT_PUBLISHED:
                log.info("id export for %s is not published yet (%d)", day, resp.status_code)
                continue
            resp.raise_for_status()
            ids = parse_series_ids(resp.content)
            log.info("id export for %s: %d series ids", day, len(ids))
            return ids
    raise RuntimeError(
        f"no TMDB id export published in the last {lookback_days + 1} days: {attempted}"
    )
