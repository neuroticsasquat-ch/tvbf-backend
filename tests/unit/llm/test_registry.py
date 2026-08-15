"""The provider registry (NEU-1098)."""

import pytest

from tvbf.llm.registry import DEEPINFRA, base_url_for


def test_the_base_url_is_a_constant_not_a_setting():
    """A base URL is a property of the provider, not of a deployment — making it
    configurable adds an env var whose only correct value is this one, plus a
    second one to get wrong (project spec §6)."""
    assert base_url_for(DEEPINFRA) == "https://api.deepinfra.com/v1/openai"


def test_an_unregistered_provider_raises_rather_than_guessing_an_endpoint():
    with pytest.raises(KeyError):
        base_url_for("openai")
