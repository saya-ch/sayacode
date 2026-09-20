"""Launch the SAYACODE 2.0 package from a source checkout."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from sayacode.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
