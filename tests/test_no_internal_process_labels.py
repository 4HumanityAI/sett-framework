"""Guard test: no internal process label appears in the public tree.

The project keeps its work plan, decisions and reviews in internal records
that use short labels: numbered work batches, numbered decisions and
options, audit-finding codes and test-plan case numbers. Those labels mean
nothing to a reader of the public framework, so public text uses plain
names that explain themselves instead. The public-naming convention in
docs/SETT_Conventions.md states the rule; this test keeps it from eroding,
for the same reason the other guard tests exist: a sweep that
is "supposed to happen" before closing new text is easy to forget.

Scope: the README and contributor documents, docs/, templates/, and the
comments, docstrings and test names under sett/, examples/, tools/ and
tests/. CHANGELOG.md is excluded on purpose: published history is never
rewritten, so released entries keep the labels they were published with.

The patterns describe label families, not a list of known labels. Ordinary
technical tokens such as UTF-8, SHA-256, BCP-47 or linter rule codes do not
match them. This file contains the patterns themselves, so it exempts
itself, the same way the other guard tests do.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

LABEL = re.compile(
    r"\b(?:"
    r"B(?:0\d|1\d)[A-Z]?\d?(?:-[A-Z0-9]+)*"  # numbered work batches
    r"|BR-\d+|L-BR\d+|L0\d|D4|D-\d{2}"  # numbered rules and decisions
    r"|S\d{2}(?:-S\d{2})?"  # numbered decision series
    r"|PF\d{2}(?:-[A-Z0-9]+)*|PF-T\d{3}"  # foundation work and its cases
    r"|AC\d{2}[A-J]?|DI\d{2}|G\d{2}(?:-G\d{2})?|V0\d|C0\d"  # test-plan cases
    r"|F\d{2}|[MLH]-0\d|API-\d{2}|AUD\d-[A-Z]+-\d{2}"  # audit findings
    r"|option[- ][CE]"  # lettered options
    r")\b"
)
LABELLED_TEST_NAME = re.compile(r"^\s*def (test_([a-z]{1,4})(\d{1,3})[a-z]?_\w*)\(")
# UTF-8 is a standard, not a label.
ALLOWED_TEST_NAME_PREFIXES = {"utf8"}


def _public_files() -> list[Path]:
    files = [ROOT / name for name in ("README.md", "CONTRIBUTING.md", "SPONSORS.md")]
    files += sorted((ROOT / "docs").glob("*.md"))
    files += sorted((ROOT / "templates").rglob("*"))
    for folder in ("sett", "examples", "tools", "tests"):
        files += sorted((ROOT / folder).rglob("*.py"))
    return [
        path
        for path in files
        if path.is_file()
        and path.suffix in {".md", ".py"}
        and "__pycache__" not in path.parts
        and path.resolve() != SELF
    ]


def test_public_text_carries_no_internal_process_label():
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{number}: {match.group()}"
        for path in _public_files()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        )
        for match in LABEL.finditer(line)
    ]
    assert not offenders, offenders


def test_test_names_do_not_start_with_an_internal_label():
    offenders = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            match = LABELLED_TEST_NAME.match(line)
            if match and match.group(2) + match.group(3) not in ALLOWED_TEST_NAME_PREFIXES:
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}:{number}: {match.group(1)}"
                )
    assert not offenders, offenders
