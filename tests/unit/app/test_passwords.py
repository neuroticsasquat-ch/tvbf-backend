import importlib

import pytest
from argon2 import PasswordHasher

from tvbf.app import passwords
from tvbf.app.passwords import hash_password, verify_password


@pytest.fixture(autouse=True)
def _production_hasher():
    """The suite-wide fixture in `tests/conftest.py` swaps in a minimum-cost
    hasher; these tests are the ones that must exercise the real parameters."""
    cheap = passwords._hasher
    passwords._hasher = PasswordHasher()
    yield
    passwords._hasher = cheap


def test_module_constructs_the_hasher_at_argon2_defaults():
    fresh = importlib.reload(passwords)
    default = PasswordHasher()
    assert (fresh._hasher.time_cost, fresh._hasher.memory_cost, fresh._hasher.parallelism) == (
        default.time_cost,
        default.memory_cost,
        default.parallelism,
    )


def test_hash_password_returns_argon2_string():
    h = hash_password("hunter2")
    assert h.startswith("$argon2id$")


def test_verify_password_correct():
    h = hash_password("hunter2")
    assert verify_password("hunter2", h) is True


def test_verify_password_wrong():
    h = hash_password("hunter2")
    assert verify_password("wrong", h) is False


def test_verify_password_handles_invalid_hash():
    assert verify_password("hunter2", "not-a-real-hash") is False


def test_two_hashes_of_same_password_differ():
    a = hash_password("hunter2")
    b = hash_password("hunter2")
    assert a != b
