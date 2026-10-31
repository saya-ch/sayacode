"""本地笔记文件。"""
from pathlib import Path


def save_note(path, title, body):
    Path(path).write_text(title.strip() + "\n" + body, encoding="utf-8")


def slug_title(title):
    return title
