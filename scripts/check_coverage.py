"""按包强制覆盖率门槛 —— 阻止安全关键模块的覆盖率退化。

整体覆盖率只是参考值；真正要守住的是「安全关键模块不许退化」。
本脚本跑一次带覆盖率的 pytest，然后按包校验门槛，任一包不达标即非零退出，
从而在 CI 中阻断。

用法：
    python scripts/check_coverage.py            # 校验门槛
    python scripts/check_coverage.py --report   # 只打印实测值（用于校准门槛）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple


ROOT = Path(__file__).resolve().parents[1]


def _force_utf8_output() -> None:
    """让本脚本的输出在非 UTF-8 控制台上也不炸。

    Windows CI 的控制台是 **cp1252**，本脚本打印的中文（「门槛」等）会抛
    ``UnicodeEncodeError`` —— 实测三个 Windows job 因此全挂，而 Ubuntu 与本地
    UTF-8 控制台完全正常；覆盖率校验本身其实是**通过**的，崩溃只发生在打印。

    ``errors="replace"`` 是刻意的：宁可少数几个字变成占位符，也不能让一个
    纯展示问题把整个 CI 判红。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

# 包 → 最低覆盖率百分比。
#
# 数值取自本次缺陷修复与测试补齐后的实测值（见下表「实测」），并留出约 3 个
# 百分点的余量：门槛表达的是「不得明显退化」，而不是「必须精确等于当前值」。
# 余量之所以留 3 点，是因为 Windows 与 Linux 上存在平台条件分支
# （例如 _read_choice_key 的 POSIX/Windows 两套实现），实测值会有小幅波动。
#
# 实测（2026-09，Windows / Python 3.13，710 passed）：
#   lib 62.0% · lib/core 72.6% · lib/models 80.6% · lib/tools 52.3% · lib/cli 40.2%
#
# lib/models 的提升轨迹：重写前 50.8% → 重写后 64.8% → 加入真实 HTTP 集成测试
# （tests/test_model_wire.py）后 79.0% → 第五轮修复补齐开关/构造路径测试后 80.1%
# → 补上探测回退的三条真实网关形状测试后 80.6%。
# 门槛每次同步上调，否则提升不会被保护。
#
# 改动此表即在改动质量门槛，请在 PR 描述里说明原因。
COVERAGE_FLOORS: Dict[str, float] = {
    "lib/core": 71.0,
    "lib/models": 79.0,
    "lib/tools": 51.0,
    "lib/cli": 39.0,
    "lib": 60.0,
}


def measure_coverage() -> Dict[str, float]:
    """跑一次带覆盖率的 pytest，返回 {包: 覆盖率%}。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        report_path = Path(tmpdir) / "coverage.json"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-x",
                "--cov=lib",
                f"--cov-report=json:{report_path}",
                "--cov-report=",
            ],
            cwd=ROOT,
            check=True,
        )
        data = json.loads(report_path.read_text(encoding="utf-8"))

    return _aggregate(data)


def _aggregate(data: dict) -> Dict[str, float]:
    """把逐文件覆盖率按包聚合。"""
    files = data.get("files", {})
    results: Dict[str, float] = {}

    for package in COVERAGE_FLOORS:
        statements = 0
        covered = 0
        for raw_path, info in files.items():
            normalized = str(raw_path).replace("\\", "/")
            if not normalized.startswith("lib/"):
                continue
            if package != "lib" and not normalized.startswith(f"{package}/"):
                continue
            summary = info.get("summary", {})
            statements += int(summary.get("num_statements", 0))
            covered += int(summary.get("covered_lines", 0))
        results[package] = (100.0 * covered / statements) if statements else 100.0

    return results


def main() -> int:
    _force_utf8_output()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        action="store_true",
        help="只打印实测覆盖率，不与门槛比较（用于校准）",
    )
    args = parser.parse_args()

    results = measure_coverage()

    print("\n按包覆盖率：")
    failures: List[Tuple[str, float, float]] = []
    for package, floor in sorted(COVERAGE_FLOORS.items()):
        actual = results[package]
        if args.report:
            print(f"  {package:<12} {actual:5.1f}%")
            continue
        passed = actual >= floor
        print(f"  [{'OK ' if passed else 'FAIL'}] {package:<12} {actual:5.1f}%  (门槛 {floor:.1f}%)")
        if not passed:
            failures.append((package, actual, floor))

    if args.report:
        return 0

    if failures:
        print("\n覆盖率门槛未达标：")
        for package, actual, floor in failures:
            print(f"  {package}: {actual:.1f}% < {floor:.1f}%")
        return 1

    print("\nCoverage floors passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
