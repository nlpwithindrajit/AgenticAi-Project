"""Fail fast, and legibly, when the app is started on the wrong interpreter.

Starting the API with a bare `uvicorn app.main:app` picks up whatever Python is
first on `PATH` — very often a conda base environment — rather than the
project's `.venv`. When that environment holds an older FastAPI beside a
current Starlette, importing the app dies inside FastAPI's own code with:

    TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'

Thirty frames of library internals, no mention of the actual problem. This
module checks the versions *before* the app is constructed and raises something
a person can act on instead.
"""

from __future__ import annotations

import sys

# FastAPI below this passes `on_startup` to Starlette's Router.__init__, which
# Starlette 1.x removed. Matches the floor in pyproject.toml.
MINIMUM_FASTAPI = (0, 141)


def _parse_version(raw: str) -> tuple[int, ...]:
    """Leading numeric components of a version string; () if unparseable."""
    parts: list[int] = []
    for chunk in raw.split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def check_runtime(
    fastapi_version: str | None = None,
    starlette_version: str | None = None,
    executable: str | None = None,
) -> None:
    """Raise `RuntimeError` if the environment cannot run this app.

    Arguments exist for testing; in normal use everything is introspected.
    """
    if fastapi_version is None or starlette_version is None:
        import fastapi
        import starlette

        fastapi_version = fastapi_version or fastapi.__version__
        starlette_version = starlette_version or starlette.__version__
    executable = executable or sys.executable

    parsed = _parse_version(fastapi_version)
    # An unparseable version is not evidence of a problem — don't block on it.
    if not parsed or parsed >= MINIMUM_FASTAPI:
        return

    minimum = ".".join(str(part) for part in MINIMUM_FASTAPI)
    raise RuntimeError(
        "\n\n"
        "This app is running on the wrong Python.\n\n"
        f"  interpreter : {executable}\n"
        f"  fastapi     : {fastapi_version}  (needs >= {minimum})\n"
        f"  starlette   : {starlette_version}\n\n"
        "FastAPI below "
        f"{minimum} is incompatible with Starlette 1.x — it fails deep inside\n"
        "FastAPI with \"Router.__init__() got an unexpected keyword argument\n"
        "'on_startup'\", which looks like an application bug but is not.\n\n"
        "Start it through the project environment instead:\n\n"
        "    make dev\n\n"
        "or, equivalently:\n\n"
        "    uv run uvicorn app.main:app --reload --reload-dir app\n\n"
        "A bare `uvicorn ...` uses whatever Python is first on PATH, which is\n"
        "usually a conda base environment. `make env` prints what you are\n"
        "actually running.\n"
    )


__all__ = ["MINIMUM_FASTAPI", "check_runtime"]
