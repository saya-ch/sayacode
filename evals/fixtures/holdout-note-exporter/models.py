"""笔记记录。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Note:
    id: int
    title: str
    body: str
    private: bool = False
