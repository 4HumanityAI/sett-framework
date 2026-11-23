"""Local files never reach the release archive, and source is never touched.

The tree built here is synthetic. Its "secret" is a marker string, used
only to prove that no byte of an environment file reaches the archive.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from zipfile import ZipFile

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "build_release_zip.py"
_spec = importlib.util.spec_from_file_location("build_release_zip_under_test", _TOOL)
brz = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brz)

MARKER = b"SYNTHETIC-MARKER-NOT-A-CREDENTIAL"

SOURCE = {
    "README.md": b"# readme\n",
    "pyproject.toml": b"[project]\n",
    "sett/__init__.py": b'__version__ = "9.9.9"\n',
    "sett/core_ruler/agent.py": b"# source\n",
    "sett/environment.py": b"# a module, not an environment file\n",
    "docs/guide.md": b"guide\n",
    "docs/env.md": b"notes about environments\n",
    "tests/test_x.py": b"def test_x():\n    pass\n",
    "examples/demo.py": b"print('demo')\n",
    ".gitignore": b".env\n",
    ".github/workflows/ci.yml": b"name: CI\n",
}

RESIDUE = {
    ".env": b"API_KEY=" + MARKER + b"\n",
    ".env.local": b"TOKEN=" + MARKER + b"\n",
    ".env.production": b"TOKEN=" + MARKER + b"\n",
    "examples/.env": b"TOKEN=" + MARKER + b"\n",
    "sett/.env.test": b"TOKEN=" + MARKER + b"\n",
    ".coverage": b"coverage-db\n",
    ".coverage.host.1234.5678": b"coverage-db\n",
    "coverage.xml": b"<coverage/>\n",
    "htmlcov/index.html": b"<html/>\n",
    ".idea/workspace.xml": b"<project/>\n",
    ".vscode/settings.json": b"{}\n",
    "env/pyvenv.cfg": b"home = x\n",
    "venv/pyvenv.cfg": b"home = x\n",
    ".venv/pyvenv.cfg": b"home = x\n",
    "notes.swp": b"swap\n",
    "docs/.guide.md.swo": b"swap\n",
    "Thumbs.db": b"thumbs\n",
    "sett/.DS_Store": b"ds\n",
    "sett/_speedups.pyd": b"binary\n",
    "sett/__pycache__/agent.cpython-314.pyc": b"\x00",
    "old_build.egg/EGG-INFO/PKG-INFO": b"Name: x\n",
    "sett_framework.egg-info/PKG-INFO": b"Name: x\n",
    ".pytest_cache/v/cache/lastfailed": b"{}\n",
    ".ruff_cache/content": b"x\n",
    "dist/sett.whl": b"x\n",
    "build/lib/sett/agent.py": b"# stale\n",
    "governance_card.json": b"{}\n",
}


def _write(root: Path, files: dict[str, bytes]) -> None:
    for relative, data in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _build(tmp_path: Path, files: dict[str, bytes]):
    root = tmp_path / "project"
    _write(root, files)
    before = _snapshot(root)
    output = tmp_path / "release.zip"
    brz.build(root, output)
    after = _snapshot(root)
    with ZipFile(output) as archive:
        names = {name.split("/", 1)[1] for name in archive.namelist()}
        payload = b"".join(archive.read(name) for name in archive.namelist())
    return names, payload, before, after


def test_local_residue_is_left_out_and_source_is_kept(tmp_path):
    names, payload, before, after = _build(tmp_path, {**SOURCE, **RESIDUE})

    assert names == set(SOURCE)
    assert MARKER not in payload
    # The builder reads; it never cleans or rewrites the source tree.
    assert before == after
    assert set(RESIDUE) <= set(after)


@pytest.mark.parametrize("relative", sorted(RESIDUE))
def test_each_residue_file_is_left_out_on_its_own(tmp_path, relative):
    names, payload, _before, _after = _build(tmp_path, {**SOURCE, relative: RESIDUE[relative]})

    assert names == set(SOURCE)
    assert MARKER not in payload


def test_names_that_only_resemble_residue_are_still_source(tmp_path):
    lookalikes = {
        "sett/environment.py": b"# module\n",
        "docs/env.md": b"doc\n",
        "sett/envelope.py": b"# module\n",
        "docs/coverage.md": b"doc\n",
        "sett/swp_reader.py": b"# module\n",
        "docs/venv_guide.md": b"doc\n",
    }
    names, _payload, _before, _after = _build(tmp_path, {**SOURCE, **lookalikes})

    assert set(lookalikes) <= names


@pytest.mark.parametrize(
    "template",
    [
        b"",
        b"# Copy to .env and fill in.\n",
        b"# comment\nANTHROPIC_API_KEY=\n\nSETT_LLM_MODEL=\n",
        b"  SPACED_NAME =   \n",
    ],
)
def test_an_empty_environment_template_is_shipped(tmp_path, template):
    names, _payload, _before, _after = _build(tmp_path, {**SOURCE, ".env.example": template})

    assert ".env.example" in names


@pytest.mark.parametrize(
    "template",
    [
        b"API_KEY=" + MARKER + b"\n",
        b"# comment\nAPI_KEY=\nTOKEN=value\n",
        b"API_KEY=your-key-here\n",
        b'API_KEY=""\n',
        b"export API_KEY\n",
        b"=value\n",
        b"\xff\xfe\n",
    ],
)
def test_a_template_that_carries_any_value_stops_the_release(tmp_path, template):
    root = tmp_path / "project"
    _write(root, {**SOURCE, ".env.example": template})
    output = tmp_path / "release.zip"

    with pytest.raises(ValueError, match=r"\.env\.example"):
        brz.build(root, output)


def test_the_real_tree_ships_no_local_residue():
    root = _TOOL.parents[1]
    shipped = [path.relative_to(root).as_posix() for path in brz.included_files(root)]

    for relative in shipped:
        name = relative.rsplit("/", 1)[-1]
        assert not (name == ".env" or name.startswith(".env.")), relative
        assert not name.startswith(".coverage"), relative
        assert not name.endswith((".swp", ".swo", ".pyc", ".pyo", ".pyd")), relative
    assert "sett/__init__.py" in shipped
    assert "README.md" in shipped
