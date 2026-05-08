"""Source checkout launcher for SAYACODE."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


def main(argv: list[str] | None = None) -> None:
    """Launch the real CLI, keeping --test as a source-checkout helper."""
    raw_args = list(sys.argv[1:] if argv is None else argv)

    if "--test" in raw_args or "-t" in raw_args:
        test_args = [arg for arg in raw_args if arg not in {"--test", "-t"}]
        _run_tests(test_args)
        return

    try:
        from lib.cli import main as cli_main

        cli_main(raw_args)
    except ImportError as exc:
        print(f"Error: could not import SAYACODE CLI: {exc}")
        print()
        print("Install dependencies first:")
        print("  python -m pip install -r requirements.txt")
        sys.exit(1)
    except KeyboardInterrupt:
        print()
        print("Interrupted by user")
        sys.exit(0)
    except Exception as exc:
        print(f"Error: {exc}")
        sys.exit(1)


def _run_tests(test_args: list[str]) -> None:
    tests_dir = project_root / "tests"
    if not tests_dir.exists():
        print("No tests directory found; skipping.")
        sys.exit(0)

    print("=" * 60)
    print("SAYACODE tests")
    print("=" * 60)
    print()

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *test_args],
        cwd=str(project_root),
        stdin=subprocess.DEVNULL,
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
