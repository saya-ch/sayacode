"""强制停止必须明确标记检查点之后的操作效果尚未确认。"""

import asyncio
from pathlib import Path

from sayacode.agent import AgentRuntime
from sayacode.host.views import task_view
from sayacode.tasks import TaskManager, WorktreeManager


async def test_shutdown_timeout_marks_unconfirmed_effects(tmp_path: Path) -> None:
    runtime = await AgentRuntime.open(tmp_path / "state")
    manager = TaskManager(runtime.store, WorktreeManager(tmp_path / "worktrees"))
    entered = asyncio.Event()

    async def never_finishes(_record, _control) -> None:
        entered.set()
        await asyncio.Event().wait()

    try:
        record = await manager.spawn(
            parent_thread_id=None,
            role="reviewer",
            prompt="检查项目",
            workspace=tmp_path,
            worktree_enabled=False,
            runner=never_finishes,
        )
        await entered.wait()
        await manager.shutdown(timeout=0.01)
        saved = await manager.get(record.task_id)
        assert saved.status == "interrupted"
        assert saved.unconfirmed_effects is True
        assert "恢复前" in str(saved.recovery_note)
        public = task_view(saved, "workspace-test")
        assert public["unconfirmed_effects"] is True
        assert public["recovery_note"] == saved.recovery_note
    finally:
        await runtime.close()
