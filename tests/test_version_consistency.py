"""
Guard test: the version declared in pyproject.toml and the
sett.__version__ string must always match.

Motivated by a real incident: a release was prepared with
pyproject.toml bumped but sett/__init__.py still carrying the previous
version, so ``sett.__version__`` reported one version behind the
installed package. This test makes that class of mismatch impossible
to ship silently.
"""

import re
from pathlib import Path

import sett


def test_no_release_artefact_name_hardcodes_a_version():
    """Extended in v0.12.0 after the packaging tool went stale.

    ``tools/build_release_zip.py`` named its default archive with a
    literal version, so a v0.12.0 release would have shipped in a file
    called v0.11.0, the same mismatch this file was written to prevent,
    one layer further out. The tool now derives the name from
    ``sett.__version__``; this makes writing a literal back in fail.
    """
    from sett.__init__ import __file__ as package_file  # noqa: F401

    tool = Path(__file__).resolve().parents[1] / "tools" / "build_release_zip.py"
    text = tool.read_text(encoding="utf-8")
    code = "\n".join(
        line
        for line in text.splitlines()
        # The module docstring explains the incident and necessarily
        # quotes the versions involved; only executable lines are checked.
        if not line.lstrip().startswith("#")
    )
    body = code.split('"""', 2)[-1]
    literals = re.findall(r'"[^"]*v\d+\.\d+\.\d+[^"]*"', body)
    assert not literals, (
        "tools/build_release_zip.py hardcodes a version in a string "
        f"literal: {literals}. Derive it from sett.__version__ instead."
    )


def test_the_packaging_tool_names_the_archive_after_the_declared_version():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    try:
        import build_release_zip
    finally:
        sys.path.pop(0)

    root = Path(__file__).resolve().parents[1]
    assert build_release_zip.declared_version(root) == sett.__version__
    assert sett.__version__ in build_release_zip.default_destination(root).name


def test_pyproject_version_matches_dunder_version():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    assert match, "pyproject.toml has no version field"
    assert match.group(1) == sett.__version__, (
        f"Version mismatch: pyproject.toml says {match.group(1)!r} but "
        f"sett.__version__ says {sett.__version__!r}. Bump BOTH before "
        f"releasing."
    )
