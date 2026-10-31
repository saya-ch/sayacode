import unittest

from service import TaskService
from store import TaskStore


class PublicTests(unittest.TestCase):
    def test_create_complete(self):
        service = TaskService(TaskStore())
        task = service.create(" run ")
        self.assertEqual(task.title, "run")
        self.assertTrue(service.complete(task.id).done)
