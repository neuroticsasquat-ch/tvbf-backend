"""Each scheduled job run is one root span named for the job (NEU-1625).

Under `opentelemetry-instrument` a job's DB and HTTP calls would otherwise each be
a trace of their own, so a run could not be found or timed by job name in SigNoz.
"""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from tvbf.config import get_settings
from tvbf.jobs import raise_test_error, weekly_recommendations
from tvbf.jobs.scheduled import scheduled_main


@pytest.fixture
def exporter(monkeypatch):
    """A private provider behind `trace.get_tracer`, so the test leaves no global tracer behind."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    get_settings.cache_clear()
    yield exporter
    get_settings.cache_clear()


def test_a_run_is_one_root_span_named_for_the_job(exporter):
    seen = {}

    async def _runner(settings):
        seen["span"] = trace.get_current_span().get_span_context()
        return True

    assert (
        scheduled_main(
            runner=_runner, healthcheck_url=lambda s: None, name="test job", job="test_job"
        )
        == 0
    )

    [span] = exporter.get_finished_spans()
    assert span.name == "job test_job"
    assert span.parent is None
    assert span.attributes == {"pipeline_mode": "test_job"}
    assert span.status.status_code is StatusCode.UNSET
    # Current inside the runner, so the job's DB and HTTP spans nest under it.
    assert seen["span"].span_id == span.get_span_context().span_id


def test_a_crashed_run_records_the_exception_on_its_span(exporter):
    assert raise_test_error.main() == 1

    [span] = exporter.get_finished_spans()
    assert span.name == "job raise_test_error"
    assert span.status.status_code is StatusCode.ERROR
    [event] = span.events
    assert event.name == "exception"
    assert event.attributes is not None
    assert event.attributes["exception.type"] == "tvbf.jobs.raise_test_error.JobTestError"


def test_the_weekly_pass_is_one_root_span_too(exporter, monkeypatch):
    async def _run(args):
        return 0

    monkeypatch.setattr(weekly_recommendations, "run", _run)
    assert weekly_recommendations.main([]) == 0

    [span] = exporter.get_finished_spans()
    assert span.name == "job weekly_recommendations"
    assert span.attributes == {"pipeline_mode": "weekly_recommendations"}
