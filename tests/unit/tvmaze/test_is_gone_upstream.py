"""The gone-vs-broken predicate (NEU-1006).

`_request` already retries timeouts, network errors, 429s and 5xx to exhaustion
before raising, so anything reaching a run loop is persistent. That is what
makes excluding 404 from the consecutive-failure count safe without weakening
outage detection.
"""

import httpx
import pytest

from tvbf.tvmaze.client import is_gone_upstream


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.tvmaze.com/shows/1")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


def test_404_is_gone():
    assert is_gone_upstream(_http_error(404)) is True


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_not_gone(status):
    """The behaviour deliberately preserved: a persistent 5xx must still abort."""
    assert is_gone_upstream(_http_error(status)) is False


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_other_4xx_is_not_gone(status):
    """A 400/401 is a bug in our request or config and must still abort.

    Absorbing those would be strictly worse than the behaviour being replaced.
    """
    assert is_gone_upstream(_http_error(status)) is False


def test_410_is_not_gone():
    """Deliberately excluded — TV Maze does not send it, so the branch would
    be unexercised. Add it when upstream does."""
    assert is_gone_upstream(_http_error(410)) is False


@pytest.mark.parametrize(
    "exc",
    [
        httpx.TimeoutException("timed out"),
        httpx.ConnectError("refused"),
        ValueError("something else entirely"),
        RuntimeError("boom"),
    ],
)
def test_non_http_status_errors_are_not_gone(exc):
    assert is_gone_upstream(exc) is False
