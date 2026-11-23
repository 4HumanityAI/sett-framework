"""Build a source ZIP with fixed, platform-independent entry metadata.

Byte reproducibility requires identical source bytes, the same source root
name, and a matching Python/zlib compression toolchain. Fixed metadata does
not promise identical compression across arbitrary toolchain versions.

The default archive name is derived from ``sett.__version__`` rather than
written out, because the version string here used to be a literal and
went stale: a v0.12.0 release would have been packaged in a file named
v0.11.0. That is the same class of mismatch ``tests/test_version_consistency.py``
was written for after a real incident, so this reads the one source that
test already guards instead of adding a third place to keep in step.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

FIXED_TIMESTAMP = (2026, 11, 13, 0, 0, 0)

# Directory names (exact match against any path component) that never
# belong in a release: VCS internals, tool caches, virtual environments,
# coverage reports, editor settings and local build byproducts. None of
# these are the release's actual source.
EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    ".idea",
    ".vscode",
}

# Directory-name suffixes (matched with str.endswith on each path
# component) for names that vary per package, like
# "sett_framework.egg-info": a fixed set can't enumerate every
# possible project name, so this checks the pattern instead.
EXCLUDED_PART_SUFFIXES = (
    ".egg-info",
    ".egg",
)

EXCLUDED_FILE_SUFFIXES = {".pyc", ".pyo", ".pyd", ".swp", ".swo"}

# Exact file names, wherever they appear, that are local artifacts
# rather than release content.
EXCLUDED_FILE_NAMES = {
    ".DS_Store",
    "Thumbs.db",
    ".coverage",
    "coverage.xml",
    "governance_card.html",
    "governance_card.json",
}

# File-name prefixes for local artifacts whose full name varies, such as
# the per-process data files coverage writes in parallel mode.
EXCLUDED_FILE_PREFIXES = (
    ".coverage.",
)

# The one environment file a release may carry: a template. Its name is
# not trusted. build() reads it and refuses the release unless every
# assignment in it has an empty value.
ENVIRONMENT_TEMPLATE_NAME = ".env.example"

# Glob patterns evaluated only against files directly at the project
# root (not recursively): MIGRATION_*.md documents a specific past
# upgrade path. The framework records upgrade notes in CHANGELOG.md and
# repository history rather than maintaining per-release migration
# guides. Excluding them unconditionally (rather than keeping the
# current version's guide) avoids the accumulation risk a future minor
# bump would otherwise carry: nothing here deletes or updates an older
# guide automatically, so a forgotten one would simply pile up in the
# repository. See CHANGELOG.md and git history for upgrade notes instead.
EXCLUDED_ROOT_GLOBS = (
    "MIGRATION_*.md",
)


def _is_excluded(path: Path, root: Path) -> bool:
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return True
    if any(
        part.endswith(suffix)
        for part in path.parts
        for suffix in EXCLUDED_PART_SUFFIXES
    ):
        return True
    if path.suffix in EXCLUDED_FILE_SUFFIXES:
        return True
    if path.name in EXCLUDED_FILE_NAMES:
        return True
    if path.name.startswith(EXCLUDED_FILE_PREFIXES):
        return True
    if _is_environment_file(path):
        return True
    return path.parent == root and any(
        path.match(pattern) for pattern in EXCLUDED_ROOT_GLOBS
    )


def _is_environment_file(path: Path) -> bool:
    """Whether a file is an environment file that may hold real values.

    ``.env`` and every ``.env.<suffix>`` are local configuration and are
    where credentials usually live, so they never ship. The single
    exception by name is the template, which is admitted only after
    ``_require_empty_environment_template()`` has read it.
    """
    name = path.name
    if name == ENVIRONMENT_TEMPLATE_NAME:
        return False
    return name == ".env" or name.startswith(".env.")


def _require_empty_environment_template(path: Path, root: Path) -> None:
    """Refuse a template that assigns a value to anything.

    A file named like a template can still contain a real credential. A
    release therefore accepts one only when each line is blank, a comment,
    or a variable name with nothing after the ``=``.
    """
    relative = path.relative_to(root).as_posix()
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{relative} is not UTF-8 text.") from error
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if not separator or not name.strip() or value.strip():
            raise ValueError(
                f"{relative} line {number} is not an empty assignment. An "
                "environment template in a release must leave every value empty."
            )


def included_files(root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and not _is_excluded(path, root)
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def build(root: Path, output: Path) -> None:
    root = root.resolve()
    output = output.resolve()
    if output.is_relative_to(root):
        raise ValueError("Release output must be outside the source tree.")
    files = included_files(root)
    if any(path.relative_to(root).parts[0].startswith("-") for path in files):
        raise ValueError("Source tree contains an option-like root artifact.")
    for path in files:
        if path.name == ENVIRONMENT_TEMPLATE_NAME:
            _require_empty_environment_template(path, root)
    prefix = root.name
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            info = ZipInfo(f"{prefix}/{relative}", FIXED_TIMESTAMP)
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3  # Unix metadata matches the fixed POSIX mode.
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)


def declared_version(root: Path) -> str:
    """Read the release version from the package, without importing it.

    Parsed rather than imported so that building an archive never depends
    on the package's own dependencies being installed.
    """
    text = (root / "sett" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if not match:
        raise SystemExit("sett/__init__.py declares no __version__.")
    return match.group(1)


def default_destination(root: Path) -> Path:
    return root.parent / f"SETT_Framework_v{declared_version(root)}.zip"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "destination", nargs="?", type=Path,
        help="Output ZIP outside the source tree (default: version-derived sibling).",
    )
    arguments = parser.parse_args(argv)
    source_root = Path(__file__).resolve().parents[1]
    destination = (
        arguments.destination.resolve()
        if arguments.destination is not None
        else default_destination(source_root)
    )
    try:
        build(source_root, destination)
    except ValueError as error:
        parser.error(str(error))
    print(destination)


if __name__ == "__main__":
    main()
