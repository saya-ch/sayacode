"""笔记导出入口。"""
import argparse
import json
from pathlib import Path

from models import Note
from render import render


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("target")
    args = parser.parse_args()
    notes = [Note(**row) for row in json.loads(Path(args.source).read_text(encoding="utf-8"))]
    Path(args.target).write_text(render(notes), encoding="utf-8")


if __name__ == "__main__":
    main()
