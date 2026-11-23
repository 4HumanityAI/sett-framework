"""Validate working correspondences and generate their pages.

A working correspondence records, for selected provisions of one external
instrument, what SETT's mechanisms do and the tests that show it, and names
what stays with the deployment. Records are JSON files in
``governance/mappings/``; their pages are generated next to them and are never
edited by hand.

The format is internal and experimental: it promises no compatibility, and a
stable public schema is not part of this version. A record is not a
compliance statement. It decides no applicability, configures nothing and
assesses no institution's requirement, and its fixed dimensions say so. It
holds no score, percentage or overall verdict. SETT never reads these records.

Usage:
    python tools/correspondence.py validate [FOLDER]
    python tools/correspondence.py generate [FOLDER]
    python tools/correspondence.py check [FOLDER]

``validate`` reports every problem. ``generate`` writes each record's page
and the index, and refuses while any record is invalid. ``check`` reports
pages that differ from what their records generate. The default folder is
this project's ``governance/mappings``. Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FOLDER = ROOT / "governance" / "mappings"
FORMAT = "sett.working_correspondence"
FORMAT_VERSION = 1
MAX_IDENTIFIER_BYTES = 128
MAX_TEXT_BYTES = 1024
MAX_ITEMS = 64
INDEX = "index.md"
USAGE = "usage: correspondence.py {validate|generate|check} [FOLDER]"
HAND_WRITTEN = {"README.md"}

DEPLOYMENT_DECIDES = "deployment_decides"
NOT_ASSESSED = "not_assessed_by_sett"
FRESHNESS = ("pending", "current", "newer_edition_not_reviewed")
APPROVED_FIELDS = ("revision", "locator", "interpretation", "criterion", "limits")

_RECORD = ("format", "format_version", "record_id", "revision", "instrument", "requirements",
           "comparisons", "approval")
_INSTRUMENT = ("title", "publisher", "edition", "identifiers", "capture_sha256", "freshness")
_FRESHNESS = ("state", "checked_on", "note")
_REQUIREMENT = ("requirement_id", *APPROVED_FIELDS, "approved_sha256", "capability", "applicability",
                "configuration", "institutional_outcome", "left_to_deployment")
_MECHANISM = ("mechanism", "reference", "evidence")
_COMPARISON = ("requirements", "relation")
_APPROVAL = ("state", "revision", "digest", "approved_on")

_NAME = re.compile(r"[a-z0-9][a-z0-9.-]*")
_QUALIFIED = re.compile(r"[a-z0-9][a-z0-9.-]*/[a-z0-9][a-z0-9.-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_EVIDENCE = re.compile(r"(tests/[A-Za-z0-9_/]+\.py)::(test_[A-Za-z0-9_]+)")


def digest(value: Any) -> str:
    """SHA-256 of a value's canonical JSON: sorted keys, compact, UTF-8."""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def record_digest(record: dict[str, Any]) -> str:
    """The digest an approval binds: the whole record except its approval."""
    return digest({key: value for key, value in record.items() if key != "approval"})


def approved_digest(requirement: dict[str, Any]) -> str:
    """The digest of a requirement's approved statements, exactly as written."""
    return digest({key: requirement.get(key) for key in APPROVED_FIELDS})


class _Check:
    """Collects every problem of one record, each with its path."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.errors: list[str] = []

    def fail(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def fields(self, value: Any, allowed: tuple[str, ...], path: str) -> bool:
        if not isinstance(value, dict):
            self.fail(path, "must be an object")
            return False
        for key in sorted(set(value) - set(allowed)):
            self.fail(f"{path}.{key}", "is not a field of this format")
        for key in allowed:
            if key not in value:
                self.fail(f"{path}.{key}", "is missing")
        return all(key in value for key in allowed)

    def text(self, value: Any, path: str, limit: int = MAX_TEXT_BYTES) -> bool:
        if not isinstance(value, str) or not value or value != value.strip():
            self.fail(path, "must be non-empty, trimmed text")
            return False
        if len(value.encode("utf-8")) > limit:
            self.fail(path, f"exceeds {limit} bytes")
            return False
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            self.fail(path, "cannot contain control characters")
            return False
        return True

    def name(self, value: Any, path: str) -> bool:
        if not self.text(value, path, MAX_IDENTIFIER_BYTES):
            return False
        if not _NAME.fullmatch(value):
            self.fail(path, "must use lowercase letters, digits, dots and hyphens")
            return False
        return True

    def items(self, value: Any, path: str, *, minimum: int = 0) -> list[Any]:
        if not isinstance(value, list):
            self.fail(path, "must be a list")
            return []
        if not minimum <= len(value) <= MAX_ITEMS:
            self.fail(path, f"must hold {minimum} to {MAX_ITEMS} items")
        return value[:MAX_ITEMS]

    def fixed(self, value: Any, expected: str, path: str) -> None:
        if value != expected:
            self.fail(path, f"must be {expected!r}: SETT does not decide it")

    def reference(self, value: Any, path: str) -> None:
        if not self.text(value, path, MAX_IDENTIFIER_BYTES):
            return
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts or not (self.root / relative).is_file():
            self.fail(path, "must name an existing file of this project")

    def evidence(self, value: Any, path: str) -> None:
        match = _EVIDENCE.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            self.fail(path, "must name a test as tests/<file>.py::<test function>")
            return
        test_file = self.root / match.group(1)
        pattern = re.compile(rf"^def {match.group(2)}\(", re.MULTILINE)
        if not test_file.is_file() or not pattern.search(test_file.read_text(encoding="utf-8")):
            self.fail(path, "names a test that does not exist in this project")


def _instrument(check: _Check, value: Any, approved: bool) -> None:
    if not check.fields(value, _INSTRUMENT, "instrument"):
        return
    for key in ("title", "publisher", "edition"):
        check.text(value[key], f"instrument.{key}")
    for index, item in enumerate(check.items(value["identifiers"], "instrument.identifiers", minimum=1)):
        check.text(item, f"instrument.identifiers[{index}]", MAX_IDENTIFIER_BYTES)
    if not isinstance(value["capture_sha256"], str) or not _SHA256.fullmatch(value["capture_sha256"]):
        check.fail("instrument.capture_sha256", "must be the captured copy's SHA-256")
    freshness = value["freshness"]
    if not check.fields(freshness, _FRESHNESS, "instrument.freshness"):
        return
    state, checked_on = freshness["state"], freshness["checked_on"]
    if state not in FRESHNESS:
        check.fail("instrument.freshness.state", f"must be one of {', '.join(FRESHNESS)}")
    elif state == "pending" and checked_on is not None:
        check.fail("instrument.freshness.checked_on", "must be null while the check is pending")
    elif state != "pending" and not (isinstance(checked_on, str) and _DATE.fullmatch(checked_on)):
        check.fail("instrument.freshness.checked_on", "must be the date of the check, YYYY-MM-DD")
    if freshness["note"] is not None:
        check.text(freshness["note"], "instrument.freshness.note")
    if approved and state == "pending":
        check.fail("instrument.freshness.state", "an approved record needs its freshness checked")


def _requirement(check: _Check, value: Any, path: str, approved: bool) -> str | None:
    if not check.fields(value, _REQUIREMENT, path):
        return None
    check.name(value["requirement_id"], f"{path}.requirement_id")
    check.text(value["revision"], f"{path}.revision", MAX_IDENTIFIER_BYTES)
    for key in ("locator", "interpretation", "criterion", "limits"):
        check.text(value[key], f"{path}.{key}")
    sealed = value["approved_sha256"]
    if sealed is None:
        if approved:
            check.fail(f"{path}.approved_sha256", "an approved record needs each interpretation approved")
    elif sealed != approved_digest(value):
        check.fail(f"{path}.approved_sha256", "differs from the approved statements; a new approval is needed")
    for index, item in enumerate(check.items(value["capability"], f"{path}.capability")):
        where = f"{path}.capability[{index}]"
        if check.fields(item, _MECHANISM, where):
            check.text(item["mechanism"], f"{where}.mechanism")
            check.reference(item["reference"], f"{where}.reference")
            for number, test in enumerate(check.items(item["evidence"], f"{where}.evidence", minimum=1)):
                check.evidence(test, f"{where}.evidence[{number}]")
    check.fixed(value["applicability"], DEPLOYMENT_DECIDES, f"{path}.applicability")
    check.fixed(value["configuration"], DEPLOYMENT_DECIDES, f"{path}.configuration")
    check.fixed(value["institutional_outcome"], NOT_ASSESSED, f"{path}.institutional_outcome")
    left = check.items(value["left_to_deployment"], f"{path}.left_to_deployment", minimum=1 if approved else 0)
    for index, item in enumerate(left):
        check.text(item, f"{path}.left_to_deployment[{index}]")
    return value["requirement_id"] if isinstance(value["requirement_id"], str) else None


def validate(record: Any, *, root: Path = ROOT) -> list[str]:
    """Every problem of one record; an empty list means it is valid.

    A candidate may be incomplete. An approved record must be complete and
    approved for exactly its revision and digest: any later change makes it
    invalid as approved until it is approved again.
    """
    check = _Check(root)
    if not check.fields(record, _RECORD, "record"):
        return check.errors
    if record["format"] != FORMAT or record["format_version"] != FORMAT_VERSION:
        check.fail("record.format", f"must be {FORMAT} version {FORMAT_VERSION}")
    check.name(record["record_id"], "record.record_id")
    revision = record["revision"]
    if type(revision) is not int or revision < 1:
        check.fail("record.revision", "must be a positive integer")
    approval = record["approval"]
    approved = False
    if check.fields(approval, _APPROVAL, "approval"):
        approved = approval["state"] == "approved"
        if approval["state"] == "pending":
            if any(approval[key] is not None for key in ("revision", "digest", "approved_on")):
                check.fail("approval", "a pending approval holds no revision, digest or date")
        elif not approved:
            check.fail("approval.state", "must be 'pending' or 'approved'")
        else:
            if approval["revision"] != revision:
                check.fail("approval.revision", "approves another revision; a new approval is needed")
            if approval["digest"] != record_digest(record):
                check.fail("approval.digest", "differs from the record; a new approval is needed")
            if not (isinstance(approval["approved_on"], str) and _DATE.fullmatch(approval["approved_on"])):
                check.fail("approval.approved_on", "must be the date of the approval, YYYY-MM-DD")
    _instrument(check, record["instrument"], approved)
    names: set[str] = set()
    for index, item in enumerate(check.items(record["requirements"], "record.requirements", minimum=1)):
        name = _requirement(check, item, f"requirements[{index}]", approved)
        if name in names:
            check.fail(f"requirements[{index}].requirement_id", "repeats another requirement")
        if name is not None:
            names.add(name)
    for index, item in enumerate(check.items(record["comparisons"], "record.comparisons")):
        where = f"comparisons[{index}]"
        if not check.fields(item, _COMPARISON, where):
            continue
        if item["relation"] != "unresolved":
            check.fail(f"{where}.relation", "must be 'unresolved': this format records no decided relation")
        pair = item["requirements"]
        if not (
            isinstance(pair, list) and len(pair) == 2 and pair[0] != pair[1]
            and all(isinstance(entry, str) and _QUALIFIED.fullmatch(entry) for entry in pair)
        ):
            check.fail(f"{where}.requirements", "must name two different requirements as <record>/<requirement>")
    return check.errors


def validate_folder(folder: Path, *, root: Path = ROOT) -> dict[str, list[str]]:
    """Validate every record of a folder, and the comparisons between them."""
    records = _load(folder)
    problems = {name: validate(record, root=root) if record is not None else ["not readable JSON"]
                for name, record in records.items()}
    known = {
        f"{record['record_id']}/{item['requirement_id']}"
        for name, record in records.items() if record is not None and not problems[name]
        for item in record["requirements"]
    }
    for name, record in records.items():
        if record is None or problems[name]:
            continue
        if record["record_id"] != Path(name).stem:
            problems[name].append(f"record.record_id: must match the file name {Path(name).stem!r}")
        for index, item in enumerate(record["comparisons"]):
            for requirement in item["requirements"]:
                if requirement not in known:
                    problems[name].append(f"comparisons[{index}]: {requirement!r} names no valid requirement")
    return problems


def _load(folder: Path) -> dict[str, Any]:
    records: dict[str, Any] = {}
    for path in sorted(folder.glob("*.json")):
        try:
            records[path.name] = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            records[path.name] = None
    return records


def _cell(value: str) -> str:
    return value.replace("|", "\\|")


def _freshness(freshness: dict[str, Any]) -> str:
    state = freshness["state"]
    text = {
        "pending": "Pending: not checked against the publisher's current edition",
        "current": f"Checked on {freshness['checked_on']}: still the current edition",
        "newer_edition_not_reviewed": (
            f"Checked on {freshness['checked_on']}: a newer edition exists and was not reviewed"
        ),
    }[state]
    return text if freshness["note"] is None else f"{text}. {freshness['note']}"


def render(record: dict[str, Any], *, name: str) -> str:
    """The page of one valid record; the same record always gives the same page."""
    instrument = record["instrument"]
    approval = record["approval"]
    state = (
        f"approved on {approval['approved_on']}" if approval["state"] == "approved"
        else "a candidate pending the owner's approval"
    )
    lines = [
        f"<!-- Generated by tools/correspondence.py from {name}, revision {record['revision']}, "
        f"digest {record_digest(record)}. Do not edit; edit the record and generate again. -->",
        "",
        f"# {instrument['title']}",
        "",
        f"Working correspondence, revision {record['revision']}, {state}.",
        "",
        "It states what SETT's mechanisms do for selected provisions of this",
        "instrument, and the tests that show it. It is not a compliance statement:",
        "it decides no applicability, configures nothing and assesses no",
        "institution's requirement. It covers only the provisions listed.",
        "",
        "| Instrument | |",
        "|---|---|",
        f"| Publisher | {_cell(instrument['publisher'])} |",
        f"| Edition | {_cell(instrument['edition'])} |",
        f"| Identifiers | {_cell(', '.join(instrument['identifiers']))} |",
        f"| Captured copy | SHA-256 `{instrument['capture_sha256']}` |",
        f"| Freshness | {_cell(_freshness(instrument['freshness']))} |",
    ]
    for item in record["requirements"]:
        lines += [
            "",
            f"## {item['locator']}",
            "",
            f"Requirement `{item['requirement_id']}`, interpretation revision {item['revision']}"
            + ("." if item["approved_sha256"] is not None else ", not yet approved."),
            "",
            f"**Interpretation.** {item['interpretation']}",
            "",
            f"**Criterion.** {item['criterion']}",
            "",
            f"**Limits.** {item['limits']}",
            "",
        ]
        if item["capability"]:
            lines += ["| What SETT provides | Reference | Evidence |", "|---|---|---|"]
            for mechanism in item["capability"]:
                tests = "<br>".join(f"`{test}`" for test in mechanism["evidence"])
                lines.append(f"| {_cell(mechanism['mechanism'])} | `{mechanism['reference']}` | {tests} |")
        else:
            lines.append("SETT provides no mechanism for this requirement.")
        lines += [
            "",
            "Applicability and configuration: decided by the deployment. "
            "Institutional outcome: not assessed by SETT.",
        ]
        if item["left_to_deployment"]:
            lines += ["", "What stays with the deployment:", ""]
            lines += [f"- {text}" for text in item["left_to_deployment"]]
    if record["comparisons"]:
        lines += ["", "## Comparisons", "",
                  "Each pair below shares a mechanism or a theme; neither is equivalent to", "the other or decided by it.", ""]
        lines += [f"- `{first}` and `{second}`: unresolved." for first, second in
                  (item["requirements"] for item in record["comparisons"])]
    return "\n".join(lines) + "\n"


def render_index(records: dict[str, dict[str, Any]]) -> str:
    """The index of every record of a folder, in file-name order."""
    lines = [
        "<!-- Generated by tools/correspondence.py. Do not edit. -->",
        "",
        "# Working correspondences",
        "",
        "Generated pages; see README.md for what they are and are not.",
        "",
        "| Instrument | Revision | Approval | Freshness |",
        "|---|---|---|---|",
    ]
    for name, record in sorted(records.items()):
        approval = record["approval"]
        lines.append(
            f"| [{_cell(record['instrument']['title'])}]({Path(name).stem}.md) | {record['revision']} | "
            f"{'approved ' + approval['approved_on'] if approval['state'] == 'approved' else 'pending'} | "
            f"{record['instrument']['freshness']['state'].replace('_', ' ')} |"
        )
    return "\n".join(lines) + "\n"


def expected_pages(folder: Path) -> dict[str, str]:
    """The pages a folder's records generate, by file name."""
    records = _load(folder)
    pages = {f"{Path(name).stem}.md": render(record, name=name) for name, record in records.items()}
    pages[INDEX] = render_index(records)
    return pages


def main(argv: list[str]) -> int:
    """Run one command; return 0 when everything is in order, 1 otherwise."""
    if not argv or argv[0] not in {"validate", "generate", "check"} or len(argv) > 2:
        print(USAGE, file=sys.stderr)
        return 2
    folder = Path(argv[1]) if len(argv) == 2 else DEFAULT_FOLDER
    problems = validate_folder(folder)
    invalid = {name: errors for name, errors in problems.items() if errors}
    for name, errors in sorted(invalid.items()):
        for error in errors:
            print(f"{name}: {error}")
    if invalid:
        return 1
    if argv[0] == "validate":
        print(f"{len(problems)} record(s) valid")
        return 0
    pages = expected_pages(folder)
    if argv[0] == "generate":
        for page, text in pages.items():
            (folder / page).write_bytes(text.encode("utf-8"))
        print(f"{len(pages)} page(s) written")
        return 0
    differences = [
        f"{page}: differs from its record" for page, text in sorted(pages.items())
        if not (folder / page).is_file() or (folder / page).read_bytes() != text.encode("utf-8")
    ]
    differences += [
        f"{path.name}: generated by no record" for path in sorted(folder.glob("*.md"))
        if path.name not in pages and path.name not in HAND_WRITTEN
    ]
    for difference in differences:
        print(difference)
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
