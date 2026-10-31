"""销售报告 CLI。"""
import argparse
import json
from pathlib import Path

from loader import load
from report import summarize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("target")
    args = parser.parse_args()
    result = summarize(load(args.source))
    Path(args.target).write_text(json.dumps({key: str(value) for key, value in result.items()}, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
