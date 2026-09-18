"""连跑多轮暴露不稳定用例（flaky），让抖动可见才可修。
判据：每轮都失败为真实失败；部分轮失败为 flaky；从未失败为稳定。
用法：
    python scripts/check_flaky.py --runs 3 --pattern tests/test_x.py
退出码：0 表示稳定或仅真实失败；1 表示发现 flaky。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Set, Tuple


ROOT = Path(__file__).resolve().parents[1]

# 解析 pytest -q 摘要行，提取失败用例名。
_FAILED_LINE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)


def _force_utf8_output() -> None:
    """让输出在非 UTF-8 控制台（Windows cp1252）上也不炸。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def run_once(pattern: str) -> Tuple[Set[str], int]:
    """跑一轮测试，返回（失败用例集合，进程退出码）。"""
    command = [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"]
    if pattern:
        command.append(pattern)
    result = subprocess.run(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
    )
    output = result.stdout or ""
    return set(_FAILED_LINE.findall(output)), result.returncode


def main() -> int:
    """连跑多轮并按判据报告结果，返回进程退出码。"""
    _force_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3, help="轮数（默认 3）")
    parser.add_argument("--pattern", default="", help="只跑匹配的路径/用例")
    args = parser.parse_args()

    runs = max(2, int(args.runs or 3))
    scope = ("（范围：" + args.pattern + "）") if args.pattern else ""
    print("")
    print("连跑 " + str(runs) + " 轮检测不稳定用例" + scope)

    history: List[Set[str]] = []
    for index in range(1, runs + 1):
        failed, code = run_once(args.pattern)
        history.append(failed)
        print("  第 " + str(index) + " 轮：" + str(len(failed)) + " 个失败（退出码 " + str(code) + "）")

    ever_failed: Set[str] = set().union(*history) if history else set()
    always_failed: Set[str] = set.intersection(*history) if history else set()
    flaky = sorted(ever_failed - always_failed)

    if not ever_failed:
        print("")
        print("未发现失败，也未发现不稳定用例。")
        return 0

    if always_failed:
        print("")
        print("每轮都失败（" + str(len(always_failed)) + " 个，属真实失败）：")
        for name in sorted(always_failed):
            print("  " + name)

    if flaky:
        print("")
        print("不稳定用例（" + str(len(flaky)) + " 个，部分轮次失败）：")
        for name in flaky:
            rounds = [str(i + 1) for i, snapshot in enumerate(history) if name in snapshot]
            print("  " + name + "  失败轮次: " + ", ".join(rounds))
        print("")
        print("不稳定的测试会消耗所有人的信任，请修掉：")
        print("  · 并发测试只让一边看表（工作线程无限等待，裁判权留在主线程）")
        print("  · 不要依赖真实时钟；用可注入时钟或事件同步")
        print("  · 不要依赖「本机装没装某个包」；用 monkeypatch 模拟")
        return 1

    print("")
    print("无不稳定用例（失败均为每轮一致的真实失败）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
