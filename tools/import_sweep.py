"""Import every module in the ``sett`` package and report the roster.

The v0.14.0 defensive audit reported 91/91 modules importable. That is
historical evidence for its identified revision and environment. This tool can
run locally; its presence in CI does not prove execution on other interpreters.
Every result must retain its own source revision and environment.

Importing is a real check, not a formality: it is what proves that a
module using an interpreter feature newer than the declared floor fails
loudly rather than at a downstream user's first call. That is not
hypothetical. Before v0.14.0 closed, ``sett/persistence/sqlite.py``
imported ``typing.Self``, which exists from 3.11, while the project
declared ``requires-python = ">=3.10"``. ``import sett`` raised
``ImportError`` on 3.10 and no test caught it, because every test ran on
3.13. Running this tool on an identified interpreter can expose such a gap;
a configured matrix alone cannot establish that it was executed.

Optional provider SDKs are deliberately not required. Adapters translate a
missing SDK into a configuration error at construction time, not at import
time, so every module here must import with nothing installed. A module
that needs a provider SDK just to be imported is itself the defect.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import pkgutil
import sys
from pathlib import Path


def module_names(package: str = "sett") -> list[str]:
    """Return the package's own modules, in a stable order.

    Discovery walks the package directory rather than the imported
    package, so a root that cannot be imported still produces the roster
    it failed on instead of a bare traceback.
    """
    specification = importlib.util.find_spec(package)
    if specification is None or not specification.submodule_search_locations:
        raise SystemExit(f"No importable package named {package!r} was found.")
    names = [package]
    names.extend(
        info.name
        for info in pkgutil.walk_packages(
            list(specification.submodule_search_locations), prefix=f"{package}."
        )
    )
    return sorted(set(names))


def sweep(names: list[str]) -> list[tuple[str, BaseException]]:
    """Import every module and collect the failures rather than the first."""
    failures: list[tuple[str, BaseException]] = []
    for name in names:
        try:
            importlib.import_module(name)
        except BaseException as error:  # noqa: BLE001 - reporting every failure is the point
            failures.append((name, error))
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "package", nargs="?", default="sett", help="Package to sweep (default: sett)."
    )
    arguments = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    names = module_names(arguments.package)
    failures = sweep(names)
    print(
        f"{len(names) - len(failures)}/{len(names)} modules importable "
        f"on Python {sys.version.split()[0]}"
    )
    for name, error in failures:
        print(f"  {name}: {type(error).__name__}: {error}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
