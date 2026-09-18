"""追问与推送测试：同一 thread 续跑、通知 drains。"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from lib.core.delegate_pool import AsyncDelegateRegistry
from lib.tools.delegate_tools import create_resume_tool


class ScriptedModel(BaseChatModel):
    replies: list = ["first", "second"]

    model_name: str = "fake"
    model_type: str = "fake"
    context_window: int = 4096

    @property
    def _llm_type(self):
        return "fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        idx = min(len(self.replies) - 1, sum(1 for _ in [messages]))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="r" + str(idx)))])

    def chat(self, messages):
        return "hi"

    def bind_tools(self, tools, **kwargs):
        return self


def _spawn_pair():
    from types import SimpleNamespace

    from lib.core.team_manager import TeamManager
    import tempfile
    from pathlib import Path

    home = Path(tempfile.mkdtemp())
    manager = TeamManager(home)
    manager.bind_supervisor_context(
        model=ScriptedModel(), workspace=home, runtime=SimpleNamespace(permissions=None), tools=[]
    )
    return manager


def test_resume_continues_same_thread(tmp_path):
    from lib.tools.delegate_tools import build_manager_resume_fn, build_manager_spawn_with_id

    manager = _spawn_pair()
    spawn = build_manager_spawn_with_id(manager, tmp_path)
    resume_fn = build_manager_resume_fn(manager)
    text, token = spawn("做甲", "reviewer")
    assert text
    out = resume_fn(token, "再讲一遍")
    assert out
    record = manager.supervisor._workers[token]
    assert record["turns"] == 2
    assert record["status"] == "completed"


def test_registry_resume_roundtrip():
    registry = AsyncDelegateRegistry(max_workers=2)
    calls = []

    def spawn(task, kind):
        return ("初版", "tok-1")

    def resume(token, follow_up):
        calls.append((token, follow_up))
        return "返工版:" + follow_up

    handle = registry.submit(spawn, "写东西", "builder", resume_fn=resume)
    job = registry.poll(handle, wait_seconds=5)
    assert job.status == "done"
    assert registry.pending_notifications() != []
    assert registry.pending_notifications() == []
    resumed = registry.resume(handle, "加个结尾")
    assert resumed.status == "running"
    final = registry.poll(handle, wait_seconds=5)
    assert final.status == "done"
    assert final.turns == 2
    assert "返工版" in final.result
    assert calls == [("tok-1", "加个结尾")]


def test_resume_tools_validation_and_notify():
    registry = AsyncDelegateRegistry(max_workers=1)
    tools = {t.name: t for t in create_resume_tool(registry.resume, registry.pending_notifications)}
    assert set(tools) == {"delegate_resume", "delegate_notifications"}
    assert "不能为空" in tools["delegate_resume"].invoke({"handle_id": "", "follow_up": "x"})
    assert "未知" in tools["delegate_resume"].invoke({"handle_id": "d_nope", "follow_up": "x"})
    handle = registry.submit(lambda task, kind: "好", "t", "builder")
    registry.poll(handle, wait_seconds=5)
    assert "不支持追问" in tools["delegate_resume"].invoke({"handle_id": handle, "follow_up": "再来"})
    out = tools["delegate_notifications"].invoke({})
    assert handle in out
    assert tools["delegate_notifications"].invoke({}) .startswith("暂无")
