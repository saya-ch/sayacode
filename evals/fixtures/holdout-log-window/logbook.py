"""日志区间、排序与保留策略。"""
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Entry:
    at: datetime
    message: str


class LogBook:
    def __init__(self, entries=()):
        self.entries = list(entries)

    def between(self, start, end):
        return [entry for entry in self.entries if start < entry.at < end]

    def recent(self, limit):
        return sorted(self.entries, key=lambda entry: entry.at)[-limit:]

    def purge_before(self, cutoff):
        before = len(self.entries)
        self.entries = [entry for entry in self.entries if entry.at > cutoff]
        return before - len(self.entries)
