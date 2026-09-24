"""源码测试类型与命令行发布门禁。

按顺序查布局锁文件编译测试风格类型和命令行可用性。
任一步失败就停下并报错，通过才算可发布。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PIN = re.compile(r"^([A-Za-z0-9_.-]+)(\[[A-Za-z0-9_,.-]+\])?==([^;\s]+)(?:\s*;\s*(.+))?$")


def check_dependency_pins() -> None:
    """保持直接依赖精确锁定并保持通用锁文件有效。
    无参数，检查通过无返回，失败直接退出并说明原因。
    本机须装好包管理工具，否则连锁文件一致性也查不了。
    """
    # 构建、运行和开发的直接依赖都须固定版本。
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requested = [
        *project["build-system"]["requires"],
        *project["project"]["dependencies"],
        *project["project"]["optional-dependencies"]["dev"],
    ]
    for raw in requested:
        if _PIN.fullmatch(raw.strip()) is None:
            raise SystemExit(f"Dependency is not an exact pin: {raw}")
    # 再看锁文件是否存在，最后调包管理工具核对锁是否过期
    if not (ROOT / "uv.lock").is_file():
        raise SystemExit("Missing uv.lock")
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("Install uv to verify the universal dependency lock")
    subprocess.run([uv, "lock", "--check"], cwd=ROOT, check=True, timeout=120)


def check_layout() -> None:
    """检查源码测试目录符合新版布局。
    无参数，通过无返回，布局不对直接退出。
    只认新包目录，旧目录残留或旧引用都算失败。
    """
    # 先看新包目录在不在，旧目录残留就不往下查
    package = ROOT / "src" / "sayacode"
    if not package.is_dir():
        raise SystemExit("Missing src/sayacode package")
    if (ROOT / "lib").exists():
        raise SystemExit("Legacy lib/ tree remains; SAYACODE packages src/sayacode only")
    old_terminal = (
        "interactive.py", "completion.py", "display.py", "help.py", "input.py",
        "selection.py", "theme.py", "turn.py", "result_views.py", "model_setup.py",
        "commands.py", "approvals.py", "memory.py", "reviewer.py", "team.py",
    )
    present = [name for name in old_terminal if (package / "cli" / name).exists()]
    if present:
        raise SystemExit("Legacy TUI modules remain: " + ", ".join(present))
    static = package / "web" / "static"
    if not (ROOT / "frontend" / "package-lock.json").is_file():
        raise SystemExit("Missing frontend/package-lock.json; run npm install in frontend")
    if not (static / "index.html").is_file() or not list((static / "assets").glob("*.js")):
        raise SystemExit("Missing Web assets; run npm ci and npm run build in frontend")
    tests = sorted((ROOT / "tests").rglob("test_*.py"))
    if not tests:
        raise SystemExit("No tests found")
    # 再全量扫描源码，看还有没有指向旧包的引用
    old_imports = []
    for path in package.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "from lib." in source or "import lib." in source:
            old_imports.append(str(path.relative_to(ROOT)))
    if old_imports:
        raise SystemExit("Legacy imports remain: " + ", ".join(old_imports))


def check_installed_version() -> None:
    """发布门禁必须验证当前解释器装的是本次清单，而非上次残留的 wheel。"""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = str(project["project"]["version"])
    try:
        installed = version("sayacode")
    except PackageNotFoundError as exc:
        raise SystemExit("SAYACODE is not installed in this interpreter") from exc
    if installed != expected:
        raise SystemExit(
            f"Installed sayacode {installed} differs from project {expected}; "
            "run uv sync --locked --extra dev"
        )


def run(*args: str, timeout: int = 600) -> None:
    """用当前解释器跑一条发布检查命令。
    参数是命令分片和超时秒数，成功无返回，失败抛错中断门禁。
    输出强制用统一编码，超时或非零退出都算不通过。
    """
    # 先拼出完整命令并打印，方便定位卡在哪一步
    command = [sys.executable, *args]
    print("+", " ".join(command), flush=True)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=timeout)


def main() -> int:
    """按固定顺序跑完所有发布门禁。
    无参数，全部通过返回零，任一步失败就直接中断。
    先查布局和依赖，再跑编译测试风格类型，最后验命令行能启动。
    """
    check_layout()
    check_dependency_pins()
    check_installed_version()
    run("scripts/check_web_source.py", timeout=30)
    # 下面按编译测试风格类型和命令行可用的顺序依次执行
    run("-m", "compileall", "-q", "src", "tests", "scripts")
    run("-m", "pytest", "-q")
    run("-m", "ruff", "check", "src", "tests", "scripts")
    run("-m", "mypy")
    run("-m", "sayacode", "--version", timeout=60)
    run("-m", "sayacode", "--help", timeout=60)
    print("SAYACODE 3.x release checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
