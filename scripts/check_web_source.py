"""确认 Git 中的 Web 构建产物与前端源码可重建结果一致。"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = "src/sayacode/web/static"


def main() -> int:
    tracked = subprocess.run(
        ["git", "ls-files", "--", STATIC],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    if f"{STATIC}/index.html" not in tracked:
        raise SystemExit("Web 首页尚未提交到 Git；源码安装将缺少界面")
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", STATIC],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if status:
        raise SystemExit("Web 构建产物与 Git 不一致，请重新构建并提交：\n" + status)
    print("Web 构建产物与 Git 一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
