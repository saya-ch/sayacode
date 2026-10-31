"""任务数据结构。"""
from dataclasses import dataclass


@dataclass
class Task:
    id: int
    title: str
    done: bool = False
    archived: bool = False
