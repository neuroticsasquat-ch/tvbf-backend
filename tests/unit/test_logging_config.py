import logging

import pytest

from tvbf.logging_config import RedactingFilter, configure_logging, redact_push_token

PUSH_URL = "http://100.119.16.113:3001/api/push/abc123?status=up&msg=ok&ping=397"


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("httpx", logging.INFO, __file__, 1, msg, args, None)


@pytest.fixture
def root_logging():
    """`configure_logging` replaces the root handlers (`force=True`); put them back after."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    root.handlers[:] = handlers
    root.setLevel(level)


def test_redact_push_token_replaces_only_the_token():
    assert redact_push_token(PUSH_URL) == (
        "http://100.119.16.113:3001/api/push/REDACTED?status=up&msg=ok&ping=397"
    )


def test_redact_push_token_leaves_other_text_alone():
    assert redact_push_token("no credentials here") == "no credentials here"


def test_filter_keeps_an_unrelated_record_untouched():
    record = _record("catalog: %d shows", 12)
    assert RedactingFilter().filter(record) is True
    assert record.getMessage() == "catalog: 12 shows"


def test_configured_root_handler_redacts_an_httpx_request_line(root_logging):
    # The shape httpx logs every request in, with the URL in args rather than msg.
    configure_logging("INFO")
    record = _record('HTTP Request: %s %s "%s %d %s"', "GET", PUSH_URL, "HTTP/1.1", 200, "OK")
    (handler,) = root_logging.handlers
    assert handler.filter(record)
    output = handler.format(record)
    assert "abc123" not in output
    assert "/api/push/REDACTED?status=up" in output
