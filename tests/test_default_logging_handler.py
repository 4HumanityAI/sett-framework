"""Library logging must stay silent until the embedding application opts in."""
import subprocess
import sys
from pathlib import Path


def test_import_installs_null_handler_without_configuring_root_logger():
    project_root = Path(__file__).resolve().parents[1]
    probe = """
import logging

assert logging.getLogger().handlers == []
import sett.core_ruler.lifecycle
assert logging.getLogger().handlers == []
assert any(
    isinstance(handler, logging.NullHandler)
    for handler in logging.getLogger("sett").handlers
)
logging.getLogger("sett.unconfigured_embedder_probe").warning("must stay silent")
"""

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_application_logging_still_receives_sett_records():
    project_root = Path(__file__).resolve().parents[1]
    probe = """
import logging
import sys

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("HOST:%(message)s"))
logging.getLogger().addHandler(handler)
import sett
logging.getLogger("sett.configured_embedder_probe").warning("visible")
"""

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "HOST:visible\n"
    assert completed.stderr == ""
