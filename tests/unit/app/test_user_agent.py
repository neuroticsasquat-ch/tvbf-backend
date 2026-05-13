"""Unit tests for the user-agent → device-label parser."""

from __future__ import annotations

import pytest

from tvbf.app.user_agent import parse_device_label


@pytest.mark.parametrize(
    ("ua", "expected"),
    [
        # macOS
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            "Safari on macOS",
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Chrome on macOS",
        ),
        # Windows
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
            "Edge on Windows",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Firefox on Windows",
        ),
        # iOS / Android / Linux
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
            "Safari on iOS",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36",
            "Chrome on Android",
        ),
        # Opera is detected before Chrome
        (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/116.0.0.0 Safari/537.36 OPR/102.0.0.0",
            "Opera on Linux",
        ),
        # Just an OS
        ("Random user agent on Windows NT 10.0", "Windows"),
        # Just a browser
        ("MyTool Firefox/1.0", "Firefox"),
        # Neither
        ("custom/1.0", "Unknown device"),
        # Empty / missing
        (None, "Unknown device"),
        ("", "Unknown device"),
    ],
)
def test_parse_device_label(ua: str | None, expected: str) -> None:
    assert parse_device_label(ua) == expected
