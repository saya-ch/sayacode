"""多 CLI 对同一会话目录的更新不会覆盖信任和记忆设置。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from sayacode.agent import AgentRuntime
from sayacode.application import SayacodeApp
from sayacode.config import ConfigRepository
from tests.support import ContractModel, contract_app


async def _second_app(first: SayacodeApp, model: ContractModel | None = None) -> SayacodeApp:
    repository = ConfigRepository(first.paths.home)
    config = await repository.load()
    runtime = await AgentRuntime.open(first.paths.home)
    app = SayacodeApp(
        paths=first.paths,
        repository=repository,
        config=config,
        runtime=runtime,
        workspace=first.workspace,
        session_id=first.session_id,
        trust_level="ask",
        profile_name=config.default_profile,
        model_override=model or ContractModel(),
    )
    return await app.initialize()


async def test_two_runtimes_keep_trust_and_memory_overrides_when_writes_overlap(
    tmp_path: Path,
) -> None:
    first = await contract_app(tmp_path, ContractModel(), session_id="shared-thread")
    await first.memory.settings({"enabled": True, "learn": "auto"})
    second = await _second_app(first)
    try:
        original_put = first.runtime.store.aput
        entered = asyncio.Event()
        continue_write = asyncio.Event()

        async def delayed_put(*args: Any, **kwargs: Any) -> Any:
            if args[0] == ("threads",) and args[1] == first.session_id:
                entered.set()
                await continue_write.wait()
            return await original_put(*args, **kwargs)

        first.runtime.store.aput = delayed_put  # type: ignore[method-assign]
        trust_task = asyncio.create_task(first.command("trust", "read_only"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        settings_task = asyncio.create_task(
            second.memory.session_settings(
                second.session_id, {"use": False, "learn": "explicit"}
            )
        )
        await asyncio.sleep(0.05)
        assert not settings_task.done()
        continue_write.set()
        await asyncio.gather(trust_task, settings_task)

        saved = await first.runtime.get_thread(first.session_id)
        assert saved is not None
        assert saved["trust_level"] == "read_only"
        assert saved["memory_use_override"] is False
        assert saved["memory_learn_override"] == "explicit"
        assert saved["memory_revoked_before"]
        assert (await second._load_thread_policy(second.session_id)).trust_level == "read_only"

        # 另一个进程仍握有旧 ask 上下文，运行目录写入也不能重新抬高信任档。
        stale_context = second._context(second.session_id, "ask")
        await second.runtime.put_thread(second.session_id, stale_context, status="idle")
        assert (await first.runtime.get_thread(first.session_id))["trust_level"] == "read_only"
    finally:
        await first.aclose()
        await second.aclose()


async def test_grant_additions_merge_and_clear_is_seen_by_other_runtime(tmp_path: Path) -> None:
    first = await contract_app(tmp_path, ContractModel(), session_id="shared-thread")
    second = await _second_app(first)
    try:
        await first._save_thread_policy(first.session_id, add_grants=("call-a",))
        await second._save_thread_policy(second.session_id, add_grants=("call-b",))
        saved = await first.runtime.get_thread(first.session_id)
        assert saved is not None and saved["session_grants"] == ["call-a", "call-b"]

        await first._save_thread_policy(first.session_id, clear_grants=True)
        refreshed = await second._load_thread_policy(second.session_id)
        assert refreshed.session_grants == set()
        assert (await second.runtime.get_thread(second.session_id))["session_grants"] == []
    finally:
        await first.aclose()
        await second.aclose()


async def test_new_turn_refreshes_global_memory_switch_changed_by_other_cli(tmp_path: Path) -> None:
    first = await contract_app(tmp_path, ContractModel(), session_id="shared-thread")
    await first.memory.settings({"enabled": True, "use": True, "learn": "explicit"})
    await first.memory.remember("以后代码注释用中文")
    model = ContractModel()
    second = await _second_app(first, model)
    try:
        assert (await second.run("写一个函数"))["status"] == "completed"
        assert "以后代码注释用中文" in str(model.received[-1])

        await first.memory.settings({"use": False})
        assert (await second.run("写另一个函数"))["status"] == "completed"
        assert second.config.memory.use is False
        assert "以后代码注释用中文" not in str(model.received[-1])

        await first.memory.settings({"enabled": False})
        assert (await second.run("检查测试"))["status"] == "completed"
        assert second.config.memory.enabled is False
    finally:
        await first.aclose()
        await second.aclose()
