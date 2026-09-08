"""Consistency checks for standards-based distribution metadata."""
from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_license_metadata_uses_the_supported_pep_639_form():
    metadata = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = metadata["project"]
    classifiers = project["classifiers"]

    assert "setuptools>=77" in metadata["build-system"]["requires"]
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert (PROJECT_ROOT / "LICENSE").is_file()
    assert not any(item.startswith("License ::") for item in classifiers)
