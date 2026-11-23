"""Working correspondences: the validator and the page generator in tools/.

The record here is synthetic. It names no real instrument, but its evidence
points at real tests of this project, which the validator resolves.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import correspondence as wc  # noqa: E402

ORIGINS_TEST = "tests/test_policy_origins.py::test_each_policy_row_keeps_its_declared_origins_and_profile"
SELECTION_TEST = "tests/test_policy_selection.py::test_all_states_represent_declarations"


def requirement(name, *, mechanism="Policy origins kept in decision records"):
    item = {
        "requirement_id": name, "revision": "1", "locator": f"Synthetic provision {name}",
        "interpretation": "Keep each selected requirement linked to its declared origin.",
        "criterion": "A decision record keeps the origins its policy declared.",
        "limits": "Recorded origins show what was declared, not that it is accurate.",
        "approved_sha256": None,
        "capability": [{"mechanism": mechanism, "reference": "docs/decision_records.md",
                        "evidence": [ORIGINS_TEST, SELECTION_TEST]}],
        "applicability": wc.DEPLOYMENT_DECIDES, "configuration": wc.DEPLOYMENT_DECIDES,
        "institutional_outcome": wc.NOT_ASSESSED,
        "left_to_deployment": ["Whether the instrument applies to the deployment."],
    }
    item["approved_sha256"] = wc.approved_digest(item)
    return item


def candidate():
    return {
        "format": wc.FORMAT, "format_version": wc.FORMAT_VERSION, "record_id": "synthetic-instrument",
        "revision": 1,
        "instrument": {
            "title": "Synthetic instrument", "publisher": "Synthetic publisher", "edition": "Edition 1",
            "identifiers": ["synthetic:1"], "capture_sha256": "0" * 64,
            "freshness": {"state": "current", "checked_on": "2026-10-18", "note": None},
        },
        "requirements": [requirement("synthetic.first"), requirement("synthetic.second")],
        "comparisons": [{"requirements": ["synthetic-instrument/synthetic.first",
                                          "synthetic-instrument/synthetic.second"],
                         "relation": "unresolved"}],
        "approval": {"state": "pending", "revision": None, "digest": None, "approved_on": None},
    }


def approve(record):
    record["approval"] = {"state": "approved", "revision": record["revision"],
                          "digest": wc.record_digest(record), "approved_on": "2026-10-18"}
    return record


def write(folder, record):
    folder.mkdir(exist_ok=True)
    (folder / f"{record['record_id']}.json").write_bytes(json.dumps(record, indent=2).encode("utf-8"))


def test_a_complete_record_is_valid_as_a_candidate_and_as_approved():
    assert wc.validate(candidate()) == []
    assert wc.validate(approve(candidate())) == []


def test_an_incomplete_candidate_is_valid_but_cannot_be_approved():
    record = candidate()
    record["instrument"]["freshness"] = {"state": "pending", "checked_on": None, "note": None}
    record["requirements"][0]["approved_sha256"] = None
    record["requirements"][1]["left_to_deployment"] = []
    assert wc.validate(record) == []
    errors = wc.validate(approve(record))
    assert any("freshness checked" in error for error in errors)
    assert any("each interpretation approved" in error for error in errors)
    assert any("left_to_deployment" in error for error in errors)


def test_a_newer_edition_is_a_new_revision_and_the_earlier_one_stays_valid():
    earlier = approve(candidate())
    later = copy.deepcopy(earlier)
    later["revision"] = 2
    later["instrument"]["edition"] = "Edition 2"
    assert wc.validate(earlier) == []
    errors = wc.validate(later)
    assert any(error.startswith("approval.revision") for error in errors)
    assert any(error.startswith("approval.digest") for error in errors)
    later["approval"] = {"state": "pending", "revision": None, "digest": None, "approved_on": None}
    assert wc.validate(later) == []


def test_generation_is_reproducible_and_a_hand_edit_is_reported(tmp_path, capsys):
    folder = tmp_path / "mappings"
    write(folder, approve(candidate()))
    record_bytes = (folder / "synthetic-instrument.json").read_bytes()
    assert wc.main(["generate", str(folder)]) == 0
    first = (folder / "synthetic-instrument.md").read_bytes()
    assert wc.main(["generate", str(folder)]) == 0
    assert (folder / "synthetic-instrument.md").read_bytes() == first
    assert wc.main(["check", str(folder)]) == 0
    (folder / "synthetic-instrument.md").write_bytes(first.replace(b"Synthetic instrument", b"Edited"))
    capsys.readouterr()
    assert wc.main(["check", str(folder)]) == 1
    assert "synthetic-instrument.md: differs from its record" in capsys.readouterr().out
    assert (folder / "synthetic-instrument.json").read_bytes() == record_bytes


def test_a_changed_interpretation_needs_a_new_approval():
    record = approve(candidate())
    record["requirements"][0]["interpretation"] = "Keep every requirement linked, and more."
    errors = wc.validate(record)
    assert any(error.startswith("requirements[0].approved_sha256") for error in errors)
    assert any(error.startswith("approval.digest") for error in errors)


@pytest.mark.parametrize(("where", "key", "value"), [
    ("record", "score", 0.9),
    ("requirement", "percentage", 80),
    ("requirement", "applicability", "applies"),
    ("requirement", "configuration", "configured"),
    ("requirement", "institutional_outcome", "met"),
])
def test_scores_percentages_and_claims_are_rejected(where, key, value):
    record = candidate()
    target = record if where == "record" else record["requirements"][0]
    target[key] = value
    assert any(key in error for error in wc.validate(record))


@pytest.mark.parametrize("evidence", [
    "tests/test_policy_origins.py::test_that_does_not_exist",
    "tests/test_missing_file.py::test_anything",
    "not a test reference",
])
def test_evidence_must_name_an_existing_test(evidence):
    record = candidate()
    record["requirements"][0]["capability"][0]["evidence"] = [evidence]
    errors = wc.validate(record)
    assert [error for error in errors if error.startswith("requirements[0].capability[0].evidence[0]")]


def test_a_pending_freshness_is_shown_as_pending():
    record = candidate()
    record["instrument"]["freshness"] = {"state": "pending", "checked_on": None, "note": None}
    page = wc.render(record, name="synthetic-instrument.json")
    assert "Pending: not checked against the publisher's current edition" in page
    assert "still the current edition" not in page
    record["instrument"]["freshness"] = {"state": "current", "checked_on": None, "note": None}
    assert any(error.startswith("instrument.freshness.checked_on") for error in wc.validate(record))


def test_requirements_sharing_a_mechanism_stay_separate(tmp_path):
    record = candidate()
    assert record["requirements"][0]["capability"][0]["mechanism"] == \
        record["requirements"][1]["capability"][0]["mechanism"]
    page = wc.render(record, name="synthetic-instrument.json")
    assert page.count("Requirement `synthetic.") == 2
    assert "`synthetic-instrument/synthetic.first` and `synthetic-instrument/synthetic.second`: unresolved." in page
    merged = candidate()
    merged["requirements"][1]["requirement_id"] = "synthetic.first"
    assert any("repeats another requirement" in error for error in wc.validate(merged))
    decided = candidate()
    decided["comparisons"][0]["relation"] = "equivalent"
    assert any(error.startswith("comparisons[0].relation") for error in wc.validate(decided))
    dangling = candidate()
    dangling["comparisons"][0]["requirements"][1] = "other-instrument/other.requirement"
    write(tmp_path, dangling)
    (problems,) = wc.validate_folder(tmp_path).values()
    assert any("names no valid requirement" in error for error in problems)


def test_unknown_fields_and_oversized_values_are_rejected():
    record = candidate()
    record["instrument"]["source_text"] = "Copied text."
    record["requirements"][0]["limits"] = "x" * (wc.MAX_TEXT_BYTES + 1)
    errors = wc.validate(record)
    assert "instrument.source_text: is not a field of this format" in errors
    assert any(error.startswith("requirements[0].limits") for error in errors)


def test_generation_refuses_an_invalid_record_and_names_stray_pages(tmp_path, capsys):
    folder = tmp_path / "mappings"
    record = candidate()
    record["requirements"][0]["institutional_outcome"] = "met"
    write(folder, record)
    assert wc.main(["generate", str(folder)]) == 1
    assert not (folder / "synthetic-instrument.md").exists()
    write(folder, candidate())
    assert wc.main(["generate", str(folder)]) == 0
    (folder / "stray.md").write_text("A page no record generates.\n", encoding="utf-8")
    capsys.readouterr()
    assert wc.main(["check", str(folder)]) == 1
    assert "stray.md: generated by no record" in capsys.readouterr().out
    assert wc.main([]) == 2


def test_the_framework_never_reads_the_working_records():
    for path in sorted((ROOT / "sett").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "governance/mappings" not in text and "correspondence" not in text, path


def test_the_project_records_are_valid_and_their_pages_current():
    assert wc.main(["validate"]) == 0
    assert wc.main(["check"]) == 0
