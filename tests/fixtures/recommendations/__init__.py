"""Real DeepInfra responses, recorded and committed (project spec §12).

`README.md` next to these files records what each one is and how it was
obtained. The loader is here so that no test reaches for a path, and so that
"the recorded response" and "the object the model actually returned" stay two
different things — `envelope` is what the provider sent, `content` is the
assistant's text, and `recommendations` is that text parsed. A test that wants
to prove the text does *not* parse asks for `content` and stops there.
"""

import json
import pathlib
from typing import Any

_DIR = pathlib.Path(__file__).parent

# The recordings, named for `scripts/record_recommendation_responses.py`'s cases.
CLEAN = "clean"
OBSCURE = "obscure"
MALFORMED = "malformed"


def envelope(name: str) -> dict[str, Any]:
    """The whole chat-completions body the provider sent, `usage` included."""
    return json.loads((_DIR / f"{name}.json").read_text(encoding="utf-8"))


def content(name: str) -> str:
    """The assistant message's text, exactly as recorded — JSON or not."""
    return envelope(name)["choices"][0]["message"]["content"]


def recommendations(name: str) -> list[dict[str, Any]]:
    """The recorded recommendations. Raises for a recording that is not JSON."""
    return json.loads(content(name))["recommendations"]
