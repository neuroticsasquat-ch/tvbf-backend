"""`python -m tvbf.push.keys` — print a fresh VAPID key set as env lines (NEU-1484).

The `vapid --gen` equivalent the project spec asks for (§5.5), minus the PEM
files: it prints the three lines to paste into `.env` or the Coolify UI, in the
base64url **raw** format `pywebpush` consumes — a 32-byte private scalar and a
65-byte uncompressed public point, the same encoding the browser's
`PushManager.subscribe({applicationServerKey})` takes.

Stdout carries the env lines and nothing else, so `task vapid:generate >> .env`
is safe; the reminder about the subject goes to stderr.

Generating a new set is a **key rotation**: every existing subscription was made
against the old public key and the push service will reject it, which the
delivery job's 404/410 rule retires over the following days (§7).
"""

import argparse
import sys

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid.utils import b64urlencode

# A placeholder rather than a guess at the maintainer's address. Push services
# may refuse a subject that is not a real `mailto:` or `https:` URL, so the
# stderr note below says to replace it.
_PLACEHOLDER_SUBJECT = "mailto:you@example.com"


def generate(subject: str) -> dict[str, str]:
    """A new key pair plus the subject, keyed by env var name."""
    # P-256 is the only curve VAPID allows (RFC 8292), and what `py_vapid`'s
    # own `generate_keys` makes; generating it here directly spares its
    # optional-typed key attributes.
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_raw = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = private_key.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    return {
        "VAPID_PRIVATE_KEY": b64urlencode(private_raw),
        "VAPID_PUBLIC_KEY": b64urlencode(public_raw),
        "VAPID_SUBJECT": subject,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tvbf.push.keys", description=__doc__)
    parser.add_argument(
        "--subject",
        help="VAPID_SUBJECT: a mailto: address or the app's https URL",
    )
    args = parser.parse_args(argv)
    for name, value in generate(args.subject or _PLACEHOLDER_SUBJECT).items():
        print(f"{name}={value}")
    if args.subject is None:
        print(
            f"note: VAPID_SUBJECT is a placeholder ({_PLACEHOLDER_SUBJECT}); "
            "replace it, or re-run with --subject",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
