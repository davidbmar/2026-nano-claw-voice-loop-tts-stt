"""The lock file must cover everything requirements.txt asks for.

The Dockerfile installs ``voice/requirements.lock`` when it exists and only
falls back to ``voice/requirements.txt`` when it does not. So adding a package
to requirements.txt alone changes nothing about the image — the code imports it
locally, passes its tests, deploys, and fails in production on the first
request that needs it.

That happened once, with pypdf: PDF upload shipped to a container that had no
PDF reader. This test is the deterministic defense.
"""

from __future__ import annotations

import re
from pathlib import Path

VOICE = Path(__file__).resolve().parents[2] / "voice"

# pip freeze normalises names: case-insensitive, and - and _ are equivalent.
_NAME = re.compile(r"^\s*([A-Za-z0-9._-]+)")


def _names(path: Path) -> set[str]:
    found = set()
    for line in path.read_text().splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        match = _NAME.match(stripped)
        if match:
            found.add(match.group(1).lower().replace("_", "-"))
    return found


def test_every_declared_requirement_is_pinned_in_the_lock():
    declared = _names(VOICE / "requirements.txt")
    locked = _names(VOICE / "requirements.lock")
    missing = sorted(declared - locked)
    assert not missing, (
        "these are in requirements.txt but not requirements.lock, so the "
        f"container will not have them: {missing}. Add a pinned version to "
        "voice/requirements.lock."
    )


def test_the_document_reader_dependencies_are_present():
    # Named explicitly because their absence is silent: extraction answers a
    # 503 that reads like a server problem rather than a missing package.
    locked = _names(VOICE / "requirements.lock")
    for package in ("pypdf", "python-docx"):
        assert package in locked, f"{package} must be pinned for document upload"
