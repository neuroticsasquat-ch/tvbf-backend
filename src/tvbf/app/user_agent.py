"""Crude user-agent → device-label mapping.

Good enough for "is anything I don't recognize signed in to my account?". Not
trying to be wru/ua-parser-js. We pick the first matching browser hint and
the first matching OS hint and stitch them together as "Browser on OS".

Kept dependency-free and pure so it's trivially unit-testable.
"""

from __future__ import annotations

# Order matters: more specific tokens first (e.g. Edge/OPR before Chrome,
# because both also embed "Chrome" in their UA strings).
_BROWSERS: tuple[tuple[str, str], ...] = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Vivaldi/", "Vivaldi"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)

# Likewise: iOS / iPadOS before generic Mac (iPhones include "like Mac OS X").
_OSES: tuple[tuple[str, str], ...] = (
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("Android", "Android"),
    ("Windows NT", "Windows"),
    ("Mac OS X", "macOS"),
    ("Macintosh", "macOS"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)


def parse_device_label(user_agent: str | None) -> str:
    """Return "Browser on OS", or a less-specific fallback if either piece
    is missing. Returns "Unknown device" when there's nothing to go on."""
    if not user_agent:
        return "Unknown device"

    browser = next((label for token, label in _BROWSERS if token in user_agent), None)
    os_name = next((label for token, label in _OSES if token in user_agent), None)

    if browser and os_name:
        return f"{browser} on {os_name}"
    if browser:
        return browser
    if os_name:
        return os_name
    return "Unknown device"
