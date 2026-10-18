"""Release and environment resolution: the instrumentation contract (NEU-1589)."""

import logging
from importlib.metadata import version

import pytest
from opentelemetry import trace
from opentelemetry.instrumentation.sqlalchemy import (
    package as sqlalchemy_instrumentation,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from packaging.requirements import Requirement

from tvbf import telemetry
from tvbf.telemetry import (
    init_sentry,
    parse_resource_attributes,
    record_handled_exception,
    resolve_telemetry,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
PROD_ATTRS = f"service.name=tvbf-backend,deployment.environment=production,service.version={SHA}"


def test_parse_resource_attributes_splits_pairs_and_percent_decodes():
    assert parse_resource_attributes("a=1, b = two%2Cparts ,,junk,c=") == {
        "a": "1",
        "b": "two,parts",
        "c": "",
    }


def test_release_and_environment_are_otels_own_resource_attributes():
    t = resolve_telemetry(
        {
            "OTEL_RESOURCE_ATTRIBUTES": PROD_ATTRS,
            # Both disagree on purpose: the resource attributes are what OTel
            # reported, so they are what Sentry must report too.
            "SOURCE_COMMIT": "someothersha",
            "ENVIRONMENT": "staging",
        }
    )
    assert t.release == t.otel_service_version == SHA
    assert t.environment == t.otel_deployment_environment == "production"


def test_prod_compose_shape_resolves_one_sha_on_both_sides():
    # What docker-compose.prod.yml produces once Coolify interpolates SOURCE_COMMIT.
    t = resolve_telemetry(
        {"OTEL_RESOURCE_ATTRIBUTES": PROD_ATTRS, "SOURCE_COMMIT": SHA, "GIT_SHA": SHA}
    )
    assert t.release == t.otel_service_version == SHA


@pytest.mark.parametrize(
    ("env", "release"),
    [
        ({"SOURCE_COMMIT": SHA, "GIT_SHA": "older"}, SHA),
        ({"GIT_SHA": SHA}, SHA),
        # Coolify's placeholder when "Include SOURCE_COMMIT in build" is off.
        ({"SOURCE_COMMIT": "unknown", "GIT_SHA": SHA}, SHA),
        ({}, "unknown"),
    ],
)
def test_release_falls_back_past_missing_and_unknown_values(env, release):
    assert resolve_telemetry(env).release == release


def test_environment_falls_back_to_environment_then_development():
    assert resolve_telemetry({"ENVIRONMENT": "production"}).environment == "production"
    assert resolve_telemetry({}).environment == "development"


def test_init_sentry_passes_the_resolved_pair_to_the_sdk(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(telemetry.sentry_sdk, "init", lambda **kw: calls.append(kw))
    init_sentry(
        {"SENTRY_DSN": "https://k@bugsink.example/1", "OTEL_RESOURCE_ATTRIBUTES": PROD_ATTRS}
    )
    assert len(calls) == 1
    assert calls[0]["release"] == SHA
    assert calls[0]["environment"] == "production"


def test_init_sentry_skips_the_sdk_without_a_dsn(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(telemetry.sentry_sdk, "init", lambda **kw: calls.append(kw))
    init_sentry({"OTEL_RESOURCE_ATTRIBUTES": PROD_ATTRS})
    assert calls == []


def test_init_sentry_logs_both_sides_of_the_contract(caplog):
    with caplog.at_level(logging.INFO, logger="tvbf.telemetry"):
        init_sentry({"OTEL_RESOURCE_ATTRIBUTES": PROD_ATTRS})
    assert (
        f"release={SHA} environment=production otel service.version={SHA} "
        "deployment.environment=production sentry=off"
    ) in caplog.text


def test_init_sentry_warns_when_exporting_without_the_contract_attributes(caplog):
    with caplog.at_level(logging.WARNING, logger="tvbf.telemetry"):
        init_sentry(
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://signoz:4318",
                "OTEL_RESOURCE_ATTRIBUTES": "service.name=tvbf-backend",
            }
        )
    assert "lacks service.version or deployment.environment" in caplog.text


def test_init_sentry_is_quiet_when_exporting_with_the_contract_attributes(caplog):
    with caplog.at_level(logging.WARNING, logger="tvbf.telemetry"):
        init_sentry(
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://signoz:4318",
                "OTEL_RESOURCE_ATTRIBUTES": PROD_ATTRS,
            }
        )
    assert caplog.text == ""


def test_otels_unknown_service_version_is_kept_rather_than_bettered():
    # Sentry reporting a SHA SigNoz never saw would break the contract's equality.
    t = resolve_telemetry({"OTEL_RESOURCE_ATTRIBUTES": "service.version=unknown", "GIT_SHA": SHA})
    assert t.release == t.otel_service_version == "unknown"


# --- SQL spans need a SQLAlchemy the instrumentation supports ---------------------


def _sqlalchemy_instrumentation_requirement() -> Requirement:
    [requirement] = [Requirement(spec) for spec in sqlalchemy_instrumentation._instruments]
    return requirement


def test_the_installed_sqlalchemy_is_one_the_instrumentation_supports():
    """The SQLAlchemy instrumentation skips itself, silently, on a version outside its
    range, and every SQL span disappears from SigNoz with no error anywhere."""
    requirement = _sqlalchemy_instrumentation_requirement()
    assert requirement.specifier.contains(version("sqlalchemy"))


@pytest.mark.xfail(
    strict=True,
    reason="opentelemetry-instrumentation-sqlalchemy 0.66b1 supports only sqlalchemy<2.1, "
    "so pyproject.toml caps sqlalchemy below 2.1. Upstream contrib #5120 (merged "
    "2026-10-14) lifts the cap to <2.2; once a release with it lands this passes, and CI "
    "fails until the sqlalchemy cap, its Dependabot ignore and this test are removed.",
)
def test_the_sqlalchemy_instrumentation_supports_sqlalchemy_2_1():
    assert _sqlalchemy_instrumentation_requirement().specifier.contains("2.1.0")


# --- handled errors on the current span (NEU-1626) ----------------------------


def _tracer_and_exporter() -> tuple[trace.Tracer, InMemorySpanExporter]:
    # A private provider rather than the global one, so the test leaves no tracer behind.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer(__name__), exporter


def test_record_handled_exception_records_on_the_current_span_and_fails_it():
    tracer, exporter = _tracer_and_exporter()
    with tracer.start_as_current_span("catalog delta"):
        record_handled_exception(ValueError("boom"))
    [span] = exporter.get_finished_spans()
    [event] = span.events
    assert event.name == "exception"
    assert event.attributes is not None
    assert event.attributes["exception.type"] == "ValueError"
    assert event.attributes["exception.message"] == "boom"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "ValueError: boom"


def test_record_handled_exception_is_a_no_op_without_a_recording_span():
    record_handled_exception(ValueError("boom"))  # no current span: must not raise


def test_record_handled_exception_leaves_an_ended_span_alone():
    # An `/admin/catalog-ingest` request span closes before the task it spawned does.
    tracer, exporter = _tracer_and_exporter()
    span = tracer.start_span("request")
    span.end()
    with trace.use_span(span, end_on_exit=False):
        record_handled_exception(ValueError("boom"))
    [finished] = exporter.get_finished_spans()
    assert finished.events == ()
    assert finished.status.status_code is StatusCode.UNSET
