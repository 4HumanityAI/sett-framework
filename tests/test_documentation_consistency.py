"""Regression checks for public documentation claims with exact rosters."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_concurrency_exception_roster_is_counted_and_named_consistently():
    text = (PROJECT_ROOT / "docs" / "api_reference.md").read_text(
        encoding="utf-8"
    )
    section = text.split("## Concurrency", 1)[1].split("## Exceptions", 1)[0]
    exceptions = (
        "UniversalMemory",
        "TraceRecorder",
        "ExecutionControlRegistry",
        "InMemoryIdempotencyStore",
        "CircuitBreaker",
    )

    assert "with five documented exceptions" in section
    assert "Outside of those five guarantees" in section
    practical = section.split("Practical implications for a deployment:", 1)[1]
    for component in exceptions:
        assert f"`{component}`" in practical
