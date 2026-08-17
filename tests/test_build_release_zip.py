"""
Tests for tools/build_release_zip.py.

Packaging hygiene: the release zip must never carry VCS internals,
tool/build caches or, per explicit policy, since this project has no
real external adopters and maintains no migration-guide convention -
MIGRATION_*.md files at the project root, regardless of the version they
claim to document. This exercises included_files()/_is_excluded() against
a small synthetic tree so a regression here is caught before it ships a
stray egg-info or cache directory inside an audit zip.
"""
from __future__ import annotations

import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_release_zip as brz  # noqa: E402


def _make_tree(root: Path) -> None:
    (root / "sett" / "core_ruler").mkdir(parents=True)
    (root / "sett" / "core_ruler" / "agent.py").write_text("# real source\n")
    (root / "sett" / "__init__.py").write_text('__version__ = "9.9.9"\n')

    (root / "README.md").write_text("# readme\n")
    (root / "MIGRATION_v0.11.md").write_text("migration guide\n")
    (root / "MIGRATION_v0.12.md").write_text("another migration guide\n")

    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    (root / "sett_framework.egg-info").mkdir()
    (root / "sett_framework.egg-info" / "PKG-INFO").write_text("Name: sett\n")

    (root / "build" / "lib" / "sett").mkdir(parents=True)
    (root / "build" / "lib" / "sett" / "agent.py").write_text("# stale copy\n")

    (root / ".pytest_cache").mkdir()
    (root / ".pytest_cache" / "v" / "cache").mkdir(parents=True)
    (root / ".pytest_cache" / "v" / "cache" / "lastfailed").write_text("{}\n")

    (root / ".mypy_cache" / "3.10").mkdir(parents=True)
    (root / ".mypy_cache" / "3.10" / "sett").mkdir(parents=True, exist_ok=True)

    (root / "sett" / "__pycache__").mkdir()
    (root / "sett" / "__pycache__" / "agent.cpython-310.pyc").write_bytes(b"\x00")
    (root / "sett" / "core_ruler" / "agent.pyo").write_bytes(b"\x00")

    (root / ".coverage").write_text("coverage-db\n")
    (root / ".DS_Store").write_bytes(b"\x00")

    (root / "docs").mkdir()
    (root / "docs" / "MIGRATION_notes_not_at_root.md").write_text(
        "should NOT be excluded: not at project root\n"
    )


@pytest.fixture
def synthetic_project(tmp_path: Path) -> Path:
    root = tmp_path / "sett-framework"
    _make_tree(root)
    return root


def test_migration_files_at_root_are_excluded_unconditionally(synthetic_project):
    """No MIGRATION_*.md at the project root ever ships, whatever version
    it claims to document; there is no per-release guide to preserve."""
    (synthetic_project / "MIGRATION_v9.9.md").write_text("current guide\n")

    files = brz.included_files(synthetic_project)
    relative = {p.relative_to(synthetic_project).as_posix() for p in files}
    assert "MIGRATION_v0.11.md" not in relative
    assert "MIGRATION_v0.12.md" not in relative
    assert "MIGRATION_v9.9.md" not in relative


def test_the_real_project_does_not_link_to_a_migration_guide(tmp_path):
    """The seam itself, checked against the actual repository: README.md
    must not promise a migration guide the release archive never ships."""
    import re

    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    linked = set(re.findall(r"\(([A-Za-z0-9_.\-]*MIGRATION[^)]*)\)", readme))
    assert not linked, (
        f"README.md links to {sorted(linked)}, but the release archive "
        f"never ships MIGRATION_*.md files. Remove the link."
    )


def test_a_migration_named_file_outside_root_is_not_touched(synthetic_project):
    """The exclusion is root-only: a real project might have unrelated
    files that happen to match the pattern deeper in the tree (e.g. a
    fixture in tests/); only the top-level release guides are dropped."""
    files = brz.included_files(synthetic_project)
    relative = {p.relative_to(synthetic_project).as_posix() for p in files}
    assert "docs/MIGRATION_notes_not_at_root.md" in relative


def test_git_internals_are_excluded(synthetic_project):
    files = brz.included_files(synthetic_project)
    relative = {p.relative_to(synthetic_project).as_posix() for p in files}
    assert not any(r.startswith(".git/") for r in relative)


def test_egg_info_directory_is_excluded_regardless_of_project_name(
    synthetic_project,
):
    files = brz.included_files(synthetic_project)
    relative = {p.relative_to(synthetic_project).as_posix() for p in files}
    assert not any("egg-info" in r for r in relative)


def test_build_directory_is_excluded(synthetic_project):
    files = brz.included_files(synthetic_project)
    relative = {p.relative_to(synthetic_project).as_posix() for p in files}
    assert not any(r.startswith("build/") for r in relative)


def test_test_and_type_caches_are_excluded(synthetic_project):
    files = brz.included_files(synthetic_project)
    relative = {p.relative_to(synthetic_project).as_posix() for p in files}
    assert not any(".pytest_cache" in r for r in relative)
    assert not any(".mypy_cache" in r for r in relative)


def test_pycache_and_compiled_python_are_excluded(synthetic_project):
    files = brz.included_files(synthetic_project)
    relative = {p.relative_to(synthetic_project).as_posix() for p in files}
    assert not any("__pycache__" in r for r in relative)
    assert not any(r.endswith(".pyc") or r.endswith(".pyo") for r in relative)


def test_coverage_and_ds_store_files_are_excluded(synthetic_project):
    files = brz.included_files(synthetic_project)
    relative = {p.relative_to(synthetic_project).as_posix() for p in files}
    assert ".coverage" not in relative
    assert ".DS_Store" not in relative


def test_real_source_and_readme_survive(synthetic_project):
    files = brz.included_files(synthetic_project)
    relative = {p.relative_to(synthetic_project).as_posix() for p in files}
    assert "sett/core_ruler/agent.py" in relative
    assert "sett/__init__.py" in relative
    assert "README.md" in relative


def test_build_writes_a_zip_containing_only_included_files(
    synthetic_project, tmp_path
):
    output = tmp_path / "out.zip"
    brz.build(synthetic_project, output)

    with ZipFile(output) as archive:
        names = set(archive.namelist())

    prefix = synthetic_project.name
    assert f"{prefix}/README.md" in names
    assert f"{prefix}/sett/core_ruler/agent.py" in names
    assert not any("MIGRATION_v0.1" in name for name in names)
    assert not any("egg-info" in name for name in names)
    assert not any(".git/" in name for name in names)


def test_declared_version_reads_without_importing_the_package(synthetic_project):
    assert brz.declared_version(synthetic_project) == "9.9.9"


def test_default_destination_uses_the_declared_version(synthetic_project):
    destination = brz.default_destination(synthetic_project)
    assert destination.name == "SETT_Framework_v9.9.9_AUDIT.zip"
    assert destination.parent == synthetic_project.parent
