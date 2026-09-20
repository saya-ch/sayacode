"""SAYACODE 2.0 source, test, type, and CLI release gate."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PIN = re.compile(r"^([A-Za-z0-9_.-]+)(\[[A-Za-z0-9_,.-]+\])?==([^;\s]+)(?:\s*;\s*(.+))?$")


def check_dependency_pins() -> None:
    """Keep exact direct pins and a current universal lock."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requested = [
        *project["project"]["dependencies"],
        *project["project"]["optional-dependencies"]["dev"],
    ]
    for raw in requested:
        if _PIN.fullmatch(raw.strip()) is None:
            raise SystemExit(f"Dependency is not an exact pin: {raw}")
    if not (ROOT / "uv.lock").is_file():
        raise SystemExit("Missing uv.lock")
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("Install uv to verify the universal dependency lock")
    subprocess.run([uv, "lock", "--check"], cwd=ROOT, check=True, timeout=120)


def check_layout() -> None:
    package = ROOT / "src" / "sayacode"
    if not package.is_dir():
        raise SystemExit("Missing src/sayacode package")
    if (ROOT / "lib").exists():
        raise SystemExit("Legacy lib/ tree remains; 2.0 packages src/sayacode only")
    tests = sorted((ROOT / "tests").rglob("test_*.py"))
    if not tests:
        raise SystemExit("No tests found")
    old_imports = []
    for path in package.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "from lib." in source or "import lib." in source:
            old_imports.append(str(path.relative_to(ROOT)))
    if old_imports:
        raise SystemExit("Legacy imports remain: " + ", ".join(old_imports))


def run(*args: str, timeout: int = 600) -> None:
    command = [sys.executable, *args]
    print("+", " ".join(command), flush=True)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=timeout)


def main() -> int:
    check_layout()
    check_dependency_pins()
    run("-m", "compileall", "-q", "src", "tests", "scripts")
    run("-m", "pytest", "-q")
    run("-m", "ruff", "check", "src", "tests", "scripts")
    run("-m", "mypy")
    run("-m", "sayacode", "--version", timeout=60)
    run("-m", "sayacode", "--help", timeout=60)
    print("SAYACODE 2.0 release checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
