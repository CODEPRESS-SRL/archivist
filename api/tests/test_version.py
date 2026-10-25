"""The server version is declared once and agrees everywhere it is exposed."""

from __future__ import annotations

import tomllib
from pathlib import Path

import app
from app.main import create_app

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
EXPECTED_VERSION = "1.2.0"


def test_version_matches_pyproject() -> None:
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]

    assert declared == app.__version__
    assert declared == create_app().version
    assert declared == EXPECTED_VERSION
