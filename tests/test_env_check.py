"""The startup guard that turns an environment problem into a readable message."""

from __future__ import annotations

import pytest

from app.env_check import MINIMUM_FASTAPI, _parse_version, check_runtime


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.141.1", (0, 141, 1)),
        ("0.115.2", (0, 115, 2)),
        ("1.6.0", (1, 6, 0)),
        ("2.0", (2, 0)),
        ("0.141.0rc1", (0, 141, 0)),
        ("", ()),
        ("not-a-version", ()),
    ],
)
def test_parse_version(raw: str, expected: tuple[int, ...]) -> None:
    assert _parse_version(raw) == expected


def test_supported_versions_pass_silently() -> None:
    check_runtime("0.141.1", "1.6.0", "/project/.venv/bin/python")
    check_runtime("0.200.0", "2.0.0", "/project/.venv/bin/python")


def test_the_real_environment_passes() -> None:
    """The venv running these tests must satisfy its own guard."""
    check_runtime()


def test_old_fastapi_is_rejected() -> None:
    with pytest.raises(RuntimeError) as exc:
        check_runtime("0.115.2", "1.3.1", "/opt/homebrew/miniforge/bin/python")

    message = str(exc.value)
    # The message has to answer: what is wrong, where, and what to do.
    assert "0.115.2" in message
    assert "/opt/homebrew/miniforge/bin/python" in message
    assert "make dev" in message
    assert "on_startup" in message, "name the symptom people will have googled"


def test_exact_minimum_is_accepted() -> None:
    minimum = ".".join(str(part) for part in MINIMUM_FASTAPI)
    check_runtime(minimum, "1.6.0", "/project/.venv/bin/python")


def test_unparseable_version_does_not_block_startup() -> None:
    """A version we cannot read is not evidence of a problem."""
    check_runtime("some-fork-build", "1.6.0", "/project/.venv/bin/python")


def test_importing_main_runs_the_guard() -> None:
    """The guard is worthless if it is not wired ahead of the FastAPI call."""
    import inspect

    import app.main

    source = inspect.getsource(app.main)
    assert source.index("check_runtime()") < source.index("app = FastAPI(")
