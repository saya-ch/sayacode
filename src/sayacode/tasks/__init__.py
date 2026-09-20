"""后台任务与工作树交付。"""

from .manager import TASK_NAMESPACE, TaskManager
from .records import TaskError, TaskPaused, TaskRecord
from .worktree import WorktreeManager, WorktreeSnapshot

__all__ = [
    "TASK_NAMESPACE",
    "TaskError",
    "TaskManager",
    "TaskPaused",
    "TaskRecord",
    "WorktreeManager",
    "WorktreeSnapshot",
]
