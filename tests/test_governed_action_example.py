"""The governed action example runs end to end with every state producer configured."""

import ast
import importlib.util
from pathlib import Path

import sett

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "governed_action.py"


def load():
    spec = importlib.util.spec_from_file_location("governed_action_example", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_example_grants_one_refund_and_blocks_the_other_two(capsys):
    load().main()
    assert capsys.readouterr().out.splitlines() == [
        f"SETT {sett.__version__}: one governed action, every state producer configured",
        "  refund of 40, customer verified      refunded",
        "  refund of 40, customer not verified  blocked: authorization.condition_unsatisfied",
        "  refund of 250, customer verified     blocked: authorization.denied",
        "Handler runs: 1",
        "Decision records: grant, deny",
        "Grant, for operators: condition example.customer_verified satisfied",
        "Blocked attempt, for the public: authorization.condition_unsatisfied, 2 values withheld",
    ]


def test_the_example_uses_only_the_standard_library_and_public_sett_modules():
    modules = set()
    for node in ast.walk(ast.parse(EXAMPLE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    assert {name.split(".")[0] for name in modules} <= {"__future__", "collections", "datetime", "typing", "sett"}
    assert not [name for name in modules if any(part.startswith("_") for part in name.split(".")[1:])]
