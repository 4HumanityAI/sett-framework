"""The declared Python support window must equal the tested one.

``pyproject.toml`` claiming a version is a promise to whoever installs
SETT. Before v0.14.0 the promise was unverified: classifiers listed
3.10 through 3.13 while the suite only ever ran on 3.13. The defensive
and infrastructure audit of v0.14.0 recorded that gap as M-03, and the
first run of a second interpreter found a real defect behind it -
``sett/persistence/sqlite.py`` imported ``typing.Self``, so ``import
sett`` raised ``ImportError`` on the 3.10 the project declared and no
test noticed.

Adding the CI matrix fixed that instance. This file fixes the class:
declaring a version and testing it are now one act, because doing only
the first fails here. The same applies in reverse, so a version can be
dropped from CI only by dropping the promise to support it.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"


def _pyproject() -> dict:
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def declared_versions() -> tuple[str, ...]:
    """Python versions named by the project's own classifiers."""
    classifiers = _pyproject()["project"]["classifiers"]
    return tuple(
        classifier.rsplit(" :: ", 1)[1]
        for classifier in classifiers
        if classifier.startswith("Programming Language :: Python :: 3.")
    )


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def matrix_versions() -> tuple[str, ...]:
    """Python versions in the test job's matrix list.

    Read with a regular expression rather than a YAML parser on purpose:
    the core installs with zero mandatory dependencies (#12) and this
    check is not worth adding one for. A restructured workflow makes the
    match fail, which is the loud outcome, not a silent pass.
    """
    match = re.search(r'^\s+python-version: \[(.+)\]$', _workflow_text(), re.MULTILINE)
    assert match, "The CI test job declares no python-version matrix list."
    return tuple(re.findall(r'"(\d+\.\d+)"', match.group(1)))


def extra_matrix_versions() -> tuple[str, ...]:
    """Python versions named by additional single-cell matrix entries."""
    return tuple(
        version
        for version in re.findall(
            r'^\s+- os: \S+\n\s+python-version: "(\d+\.\d+)"$',
            _workflow_text(),
            re.MULTILINE,
        )
    )


def test_the_ci_matrix_lists_exactly_the_declared_versions():
    assert matrix_versions() == declared_versions()


def test_every_extra_matrix_cell_uses_a_declared_version():
    assert set(extra_matrix_versions()) <= set(declared_versions())


def test_requires_python_names_the_lowest_declared_version():
    requires = _pyproject()["project"]["requires-python"]
    assert requires == f">={min(declared_versions(), key=_as_tuple)}"


def test_ruff_targets_the_lowest_declared_version():
    """Linting against a newer floor would miss what the floor rejects."""
    target = _pyproject()["tool"]["ruff"]["target-version"]
    lowest = min(declared_versions(), key=_as_tuple)
    assert target == "py" + lowest.replace(".", "")


def test_the_matrix_runs_on_more_than_one_operating_system():
    """M-02 and the release builder both have platform-dependent behaviour."""
    match = re.search(r'^\s+os: \[(.+)\]$', _workflow_text(), re.MULTILINE)
    assert match, "The CI test job declares no operating-system matrix list."
    systems = tuple(name.strip() for name in match.group(1).split(","))
    assert len(systems) >= 2
    assert any(name.startswith("ubuntu") for name in systems)
    assert any(name.startswith("windows") for name in systems)


def test_the_public_requirement_text_names_the_declared_versions():
    getting_started = (PROJECT_ROOT / "docs" / "getting_started.md").read_text(
        encoding="utf-8"
    )
    requirements = getting_started.split("## Requirements", 1)[1].split("---", 1)[0]
    for version in declared_versions():
        assert version in requirements

    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    badge = next(
        line for line in readme.splitlines() if line.startswith("[![Python ")
    )
    for version in declared_versions():
        assert version.replace(".", "%2E") in badge or version in badge


def _as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))
