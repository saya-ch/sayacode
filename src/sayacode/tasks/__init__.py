"""后台任务与工作树交付，只做重导出，不放逻辑。

任务档案管状态，任务管理管执行，工作树管隔离交付。
协调模块管父子唤醒，外部只从这里拿公开符号。"""

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
