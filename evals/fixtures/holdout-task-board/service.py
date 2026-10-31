"""任务业务入口。"""


class TaskService:
    def __init__(self, store):
        self.store = store

    def create(self, title):
        title = title.strip()
        if not title:
            raise ValueError("empty title")
        return self.store.create(title)

    def complete(self, task_id):
        task = self.store.get(task_id)
        task.done = True
        return task

    def active(self):
        return [task for task in self.store.all() if not task.archived]
