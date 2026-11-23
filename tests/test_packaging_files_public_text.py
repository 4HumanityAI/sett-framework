"""Packaging and CI files are public text too.

The process-label guard covers documents and source. These three files sit
outside its scope and are read by anyone who opens the repository, so the
same rule is checked for them here: no review-finding code and no bare
tracker reference.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = (".github/workflows/ci.yml", "MANIFEST.in", "pyproject.toml")

FINDING_CODE = re.compile(r"\b[MLH]-0\d\b")
TRACKER_REFERENCE = re.compile(r"\(#\d+\)")


@pytest.mark.parametrize("relative", FILES)
def test_packaging_file_carries_no_internal_review_label(relative):
    offenders = [
        f"{relative}:{number}: {line.strip()}"
        for number, line in enumerate(
            (ROOT / relative).read_text(encoding="utf-8").splitlines(), 1
        )
        if FINDING_CODE.search(line) or TRACKER_REFERENCE.search(line)
    ]
    assert not offenders, "\n".join(offenders)


# Built from parts so this file does not itself contain a label.
_SAMPLE_LABELS = ["M-" + "03", "L-" + "02", "H-" + "01", "(#" + "12)"]


@pytest.mark.parametrize("text", [f"raised that as {label}, and" for label in _SAMPLE_LABELS])
def test_the_patterns_catch_the_labels_they_exist_for(text):
    assert FINDING_CODE.search(text) or TRACKER_REFERENCE.search(text)


@pytest.mark.parametrize(
    "text", ["UTF-8", "SHA-256", "python-version: 3.13", "requests>=2.31.0", "#12 is not a label"]
)
def test_the_patterns_leave_ordinary_text_alone(text):
    assert not (FINDING_CODE.search(text) or TRACKER_REFERENCE.search(text))
