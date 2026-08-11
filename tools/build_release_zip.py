"""Build a deterministic source ZIP for the current SETT release.

The default archive name is derived from ``sett.__version__`` rather than
written out, because the version string here used to be a literal and
went stale: a v0.12.0 release would have been packaged in a file named
v0.11.0. That is the same class of mismatch ``tests/test_version_consistency.py``
was written for after a real incident, so this reads the one source that
test already guards instead of adding a third place to keep in step.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

FIXED_TIMESTAMP = (2026, 7, 29, 0, 0, 0)

# Directory names (exact match against any path component) that never
# belong in a release: VCS internals, tool caches, and local build
# byproducts. None of these are the release's actual source.
EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".venv",
    "venv",
    "__pycache__",
    "build",
    "dist",
}

# Directory-name suffixes (matched with str.endswith on each path
# component) for names that vary per package, like
# "sett_framework.egg-info": a fixed set can't enumerate every
# possible project name, so this checks the pattern instead.
EXCLUDED_PART_SUFFIXES = (
    ".egg-info",
)

EXCLUDED_FILE_SUFFIXES = {".pyc", ".pyo"}

# Exact file names, wherever they appear, that are local artifacts
# rather than release content.
EXCLUDED_FILE_NAMES = {
    ".DS_Store",
    ".coverage",
}

# Glob patterns evaluated only against files directly at the project
# root (not recursively): MIGRATION_*.md documents a specific past
# upgrade path. This project has no real external adopters yet, and no
# release has ever shipped, so there is no obligatory convention to
# preserve here - the framework does not maintain per-release migration
# guides at all. Excluding them unconditionally (rather than keeping the
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
    if path.parent == root and any(
        path.match(pattern) for pattern in EXCLUDED_ROOT_GLOBS
    ):
        return True
    return False


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
    prefix = root.name
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in included_files(root):
            relative = path.relative_to(root).as_posix()
            info = ZipInfo(f"{prefix}/{relative}", FIXED_TIMESTAMP)
            info.compress_type = ZIP_DEFLATED
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
    return root.parent / f"SETT_Framework_v{declared_version(root)}_AUDIT.zip"


if __name__ == "__main__":
    source_root = Path(__file__).resolve().parents[1]
    destination = (
        Path(sys.argv[1]).resolve()
        if len(sys.argv) > 1
        else default_destination(source_root)
    )
    build(source_root, destination)
    print(destination)
