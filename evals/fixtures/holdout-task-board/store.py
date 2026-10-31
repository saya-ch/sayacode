"""内存任务仓库。"""


class TaskStore:
    def __init__(self):
        self._tasks = {}
        self._next_id = 1

    def create(self, title):
        from models import Task
        task = Task(self._next_id, title)
        self._next_id += 1
        self._tasks[task.id] = task
        return task

    def get(self, task_id):
        return self._tasks[task_id]

    def all(self):
        return list(self._tasks.values())
