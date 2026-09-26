"""`python -m tvbf.push.keys` — the generated set is what pywebpush consumes (NEU-1484)."""

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02
from py_vapid.utils import b64urldecode

from tvbf.push.keys import generate, main


def test_generated_private_key_loads_and_derives_the_generated_public_key():
    keys = generate("mailto:ops@example.com")
    # `from_string` is the path `pywebpush.webpush` takes for a string key.
    loaded = Vapid02.from_string(private_key=keys["VAPID_PRIVATE_KEY"])
    assert loaded.public_key is not None
    derived = loaded.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    assert derived == b64urldecode(keys["VAPID_PUBLIC_KEY"].encode())


def test_keys_are_raw_unpadded_base64url():
    keys = generate("mailto:ops@example.com")
    assert len(b64urldecode(keys["VAPID_PRIVATE_KEY"].encode())) == 32
    public = b64urldecode(keys["VAPID_PUBLIC_KEY"].encode())
    assert len(public) == 65 and public[0] == 0x04  # uncompressed P-256 point
    for value in (keys["VAPID_PRIVATE_KEY"], keys["VAPID_PUBLIC_KEY"]):
        assert "=" not in value and "+" not in value and "/" not in value


def test_each_run_generates_a_new_pair():
    assert generate("x")["VAPID_PRIVATE_KEY"] != generate("x")["VAPID_PRIVATE_KEY"]


def test_main_prints_exactly_the_three_env_lines_on_stdout(capsys):
    assert main(["--subject", "mailto:ops@example.com"]) == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert [line.split("=", 1)[0] for line in lines] == [
        "VAPID_PRIVATE_KEY",
        "VAPID_PUBLIC_KEY",
        "VAPID_SUBJECT",
    ]
    assert lines[2] == "VAPID_SUBJECT=mailto:ops@example.com"
    assert err == ""


def test_main_without_a_subject_warns_on_stderr_only(capsys):
    assert main([]) == 0
    out, err = capsys.readouterr()
    assert "VAPID_SUBJECT=mailto:you@example.com" in out
    assert "note:" not in out
    assert "placeholder" in err
