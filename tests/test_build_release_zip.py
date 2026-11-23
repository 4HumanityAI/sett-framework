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

import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile
from zipfile import ZipInfo as StandardZipInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_release_zip as brz


def test_only_generated_governance_card_names_are_excluded(tmp_path):
    root = tmp_path / "project"
    expected = set()
    for folder in (root, root / "examples", root / "governance" / "nested"):
        folder.mkdir(parents=True)
        for name in ("governance_card.html", "governance_card.json", "source.html", "record.json"):
            (folder / name).write_text("fixture", encoding="ascii")
            if name in {"source.html", "record.json"}:
                expected.add("project/" + (folder / name).relative_to(root).as_posix())
    output = tmp_path / "release.zip"
    brz.build(root, output)
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == expected


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
    assert not any(r.endswith((".pyc", ".pyo")) for r in relative)


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
    assert destination.name == "SETT_Framework_v9.9.9.zip"
    assert destination.parent == synthetic_project.parent


def test_build_metadata_is_independent_of_zipinfo_host_default(
    synthetic_project, tmp_path, monkeypatch
):
    archives = []
    for host_system in (0, 3):
        def host_zip_info(*args, system=host_system, **kwargs):
            info = StandardZipInfo(*args, **kwargs)
            info.create_system = system
            return info

        monkeypatch.setattr(brz, "ZipInfo", host_zip_info)
        output = tmp_path / f"host-{host_system}.zip"
        brz.build(synthetic_project, output)
        archives.append(output.read_bytes())
        with ZipFile(output) as archive:
            for info in archive.infolist():
                assert info.create_system == 3
                assert info.external_attr == 0o100644 << 16
                assert info.date_time == brz.FIXED_TIMESTAMP
    assert archives[0] == archives[1]


@pytest.mark.parametrize("arguments", [["--help"], ["--unknown"], ["a.zip", "b.zip"]])
def test_cli_help_and_invalid_arguments_never_build(
    synthetic_project, monkeypatch, capsys, arguments
):
    def forbidden_build(*args):
        pytest.fail("Argument handling must finish before opening an archive.")

    monkeypatch.setattr(brz, "build", forbidden_build)
    monkeypatch.chdir(synthetic_project)
    with pytest.raises(SystemExit) as caught:
        brz.main(arguments)
    assert caught.value.code == (0 if arguments == ["--help"] else 2)
    output = capsys.readouterr()
    assert "usage:" in (output.out + output.err)
    assert not (synthetic_project / "--help").exists()


def test_build_rejects_an_output_inside_source_before_overwriting(
    synthetic_project,
):
    output = synthetic_project / "README.md"
    original = output.read_bytes()
    with pytest.raises(ValueError, match="outside the source tree"):
        brz.build(synthetic_project, output)
    assert output.read_bytes() == original


def test_build_rejects_an_existing_option_named_artifact_before_writing(
    synthetic_project, tmp_path,
):
    (synthetic_project / "--help").write_bytes(b"old accidental archive")
    output = tmp_path / "release.zip"
    output.write_bytes(b"previous release")
    with pytest.raises(ValueError, match="option-like"):
        brz.build(synthetic_project, output)
    assert output.read_bytes() == b"previous release"


def test_cli_accepts_an_explicit_destination(tmp_path, monkeypatch):
    output = tmp_path / "release.zip"
    calls = []
    monkeypatch.setattr(brz, "build", lambda root, target: calls.append((root, target)))
    brz.main([str(output)])
    assert calls == [(Path(brz.__file__).resolve().parents[1], output.resolve())]


def test_cli_preserves_the_version_derived_default(monkeypatch):
    calls = []
    monkeypatch.setattr(brz, "build", lambda root, target: calls.append((root, target)))
    brz.main([])
    root = Path(brz.__file__).resolve().parents[1]
    assert calls == [(root, brz.default_destination(root))]


def test_script_help_prints_usage_without_creating_a_file(tmp_path):
    result = subprocess.run(
        [sys.executable, "-B", brz.__file__, "--help"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    assert "usage:" in result.stdout
    assert not list(tmp_path.iterdir())


# ── the archive ships raw bytes, so line endings are release content ────

CRLF = b"\r\n"
BINARY_RELEASE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".pdf", ".zip"})


def _release_text_files_with_crlf(root: Path) -> list[str]:
    """Check every release file except explicitly declared binary formats."""
    return sorted(
        path.relative_to(root).as_posix()
        for path in brz.included_files(root)
        if path.suffix.lower() not in BINARY_RELEASE_SUFFIXES
        and CRLF in path.read_bytes()
    )


@pytest.mark.parametrize(
    "name",
    ["source.py", "README.md", "record.json", "pyproject.toml", "ci.yml",
     ".gitattributes", "LICENSE", "notes.unknown"],
)
def test_release_line_ending_guard_still_detects_text_crlf(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"first\r\nsecond\r\n")
    (tmp_path / "clean.txt").write_bytes(b"first\nsecond\n")
    assert _release_text_files_with_crlf(tmp_path) == [name]


@pytest.mark.parametrize("suffix", sorted(BINARY_RELEASE_SUFFIXES))
def test_release_line_ending_guard_exempts_declared_binary_formats(tmp_path, suffix):
    path = tmp_path / ("asset" + suffix)
    # CRLF can be binary data; the guard does not validate media formats.
    payload = b"\x00\xff\r\n\x1a\n"
    path.write_bytes(payload)
    (tmp_path / "source.py").write_bytes(b"value = 1\r\n")
    assert _release_text_files_with_crlf(tmp_path) == ["source.py"]
    assert path.read_bytes() == payload


def test_release_line_ending_guard_does_not_skip_undecodable_text(tmp_path):
    (tmp_path / "source.py").write_bytes(b"\xff\r\n")
    assert _release_text_files_with_crlf(tmp_path) == ["source.py"]


def test_release_binary_formats_match_declared_git_attributes():
    root = Path(__file__).resolve().parents[1]
    declarations = {
        fields[0][1:]
        for line in (root / ".gitattributes").read_text(encoding="utf-8").splitlines()
        if len(fields := line.split()) == 2
        and fields[0].startswith("*.")
        and fields[1] == "binary"
    }
    assert declarations == BINARY_RELEASE_SUFFIXES


def test_no_release_file_carries_windows_line_endings():
    """`.gitattributes` normalizes a checkout; it does not normalize a build.

    ``build()`` copies each file's bytes verbatim. A maintainer editing on
    Windows with a tool that writes CRLF therefore ships CRLF, whatever
    ``.gitattributes`` says, and the archive stops matching the checkout
    everyone else has. That happened while preparing v0.14.0: a scripted
    documentation edit rewrote twenty files with CRLF and nothing in the
    suite objected. The archive is the artifact under review, so the check
    belongs to the archive. Binary formats declared in `.gitattributes` are
    outside this text check: CRLF bytes can be part of their file format.
    """
    root = Path(__file__).resolve().parents[1]
    offenders = _release_text_files_with_crlf(root)
    assert not offenders, (
        "These release files carry CRLF line endings and would ship that "
        f"way: {offenders}. Rewrite them with LF."
    )


def test_the_release_archive_carries_the_templates_and_the_workflow():
    """The documented access path for both is the source tree.

    `templates/` is not packaged in the wheel by design, so the release
    archive and the checkout are the only places it exists. Saying so in
    `templates/README.md` is worth nothing if a future exclusion rule
    quietly drops the directory, and the same applies to the CI workflow
    that proves the declared Python range.
    """
    root = Path(__file__).resolve().parents[1]
    included = {
        path.relative_to(root).as_posix() for path in brz.included_files(root)
    }

    assert "templates/README.md" in included
    assert "templates/adapter_template.py" in included
    assert "templates/agent_template.py" in included
    assert "templates/expert_template.py" in included
    assert "templates/phrasing_expert_template.py" in included
    assert ".github/workflows/ci.yml" in included
