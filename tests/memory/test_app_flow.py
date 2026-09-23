"""跨会话记忆在真实 Agent 图与 Store 上的产品行为。"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from sayacode.config import ConfigRepository
from sayacode.memory.learning import MemoryLearner
from sayacode.memory.records import MemoryChange, MemorySource
from tests.agent.test_provider_http import completion, mock_provider
from tests.support import ContractModel, contract_app


class SourceAwareMemoryModel(BaseChatModel):
    """按真实来源 ID 返回 LangMem 工具调用，不替换记忆提取器。"""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "memory-integration"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        text = str(messages)
        match = re.search(r"证据 ID: (user-[a-f0-9]+)", text)
        if "跨项目适用的用户长期偏好" in text and match:
            response = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "MemoryContent",
                        "args": {
                            "subject": "注释语言",
                            "text": "以后代码注释使用中文",
                            "scope_kind": "user",
                            "evidence_ids": [match.group(1)],
                        },
                        "id": "memory-integration-1",
                    }
                ],
            )
        else:
            response = AIMessage(content="")
        response = response.model_copy(
            update={"usage_metadata": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19}}
        )
        return ChatResult(generations=[ChatGeneration(message=response)])

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools, **kwargs)


class PreferenceLearner:
    """只替换额外的记忆模型判断，主 Agent 仍跑真实图。"""

    async def extract(
        self, messages, existing, *, scope_kind, source_ids, verified_source_ids=(), callbacks=()
    ):
        if scope_kind != "user":
            return []
        return [
            MemoryChange(
                action="insert",
                subject="注释语言",
                text="用户要求代码注释使用中文",
                evidence_refs=(source_ids[0],),
            )
        ]


class ChangingFileLearner:
    def __init__(self, target: Path, *, change_file: bool) -> None:
        self.target = target
        self.change_file = change_file

    async def extract(
        self, messages, existing, *, scope_kind, source_ids, verified_source_ids=(), callbacks=()
    ):
        if scope_kind != "project":
            return []
        if self.change_file:
            self.target.write_text("新内容", encoding="utf-8")
        return [
            MemoryChange(
                action="insert",
                subject="项目说明",
                text="项目说明文件记录了构建约定",
                evidence_refs=(source_ids[1],),
            )
        ]


async def test_explicit_memory_reaches_another_session_without_copying_history(
    tmp_path: Path,
) -> None:
    first = await contract_app(tmp_path, ContractModel(), session_id="first-thread")
    try:
        saved = await first.command("memory", "remember user 注释请用中文")
        assert saved["state"] == "active"
    finally:
        await first.aclose()

    model = ContractModel()
    second = await contract_app(tmp_path, model, session_id="second-thread")
    try:
        assert (await second.run("解释这个函数"))["status"] == "completed"
        system = str(model.received[0][0].content)
        assert "注释请用中文" not in system
        assert any("注释请用中文" in str(message.content) for message in model.received[0][1:])
        handle, _ = await second._context_for_thread(second.session_id)
        snapshot = await second.runtime.get_state(handle, second.session_id)
        assert all("first-thread" not in str(message.content) for message in snapshot.values["messages"])
    finally:
        await second.aclose()


async def test_completed_turn_learns_once_and_reuses_the_memory(tmp_path: Path) -> None:
    first = await contract_app(tmp_path, ContractModel(), session_id="learn-thread")
    try:
        first.memory.learning._learner_factory = lambda _model: PreferenceLearner()
        await first.memory.settings({"enabled": True, "learn": "auto", "idle_seconds": 0})
        assert (await first.run("以后代码注释使用中文"))["status"] == "completed"
        assert await first.memory.drain(5) == ()
        user_scope, project_scope = first.memory.repository.scopes_for(first.workspace)
        assert len((await first.memory.repository.aread(user_scope)).pending) == 0
        assert len((await first.memory.repository.aread(project_scope)).pending) == 0
        records = await first.memory.list()
        assert len(records) == 1 and records[0]["state"] == "active"
        detail = await first.memory.get(records[0]["id"])
        assert any("以后代码注释使用中文" in item["preview"] for item in detail["evidence"])
        handle, context = await first._context_for_thread(first.session_id)
        await first.memory.on_turn_complete(handle, context)
        assert len(await first.memory.list()) == 1
    finally:
        await first.aclose()

    model = ContractModel()
    second = await contract_app(tmp_path, model, session_id="later-thread")
    try:
        assert (await second.run("写一个解析器"))["status"] == "completed"
        assert any(
            "用户要求代码注释使用中文" in str(message.content)
            for message in model.received[0][1:]
        )
    finally:
        await second.aclose()


async def test_read_only_turn_can_read_but_does_not_learn(tmp_path: Path) -> None:
    model = ContractModel()
    app = await contract_app(tmp_path, model)
    try:
        await app.command("memory", "remember user 注释请用中文")
        await app.memory.settings({"learn": "auto", "idle_seconds": 0})
        await app.command("trust", "read_only")
        assert (await app.run("检查项目"))["status"] == "completed"
        assert any("注释请用中文" in str(message.content) for message in model.received[0][1:])
        assert (await app.memory.status())["pending"] == 0
    finally:
        await app.aclose()


async def test_memory_extraction_failure_does_not_fail_the_agent_turn(tmp_path: Path) -> None:
    class FailingLearner:
        async def extract(self, *args, **kwargs):
            raise RuntimeError("simulated memory model failure")

    app = await contract_app(tmp_path, ContractModel())
    try:
        app.memory.learning._learner_factory = lambda _model: FailingLearner()
        await app.memory.settings({"enabled": True, "learn": "auto", "idle_seconds": 0})
        assert (await app.run("分析项目"))["status"] == "completed"
        await app.memory.drain(5)
        assert (await app.memory.status())["pending"] > 0
        assert await app.memory.list() == []
    finally:
        await app.aclose()


async def test_streamed_turn_saves_a_recoverable_source(tmp_path: Path) -> None:
    app = await contract_app(tmp_path, ContractModel())
    try:
        await app.memory.settings({"enabled": True, "learn": "auto", "idle_seconds": 300})
        events = [event async for event in app.stream("以后注释用中文")]
        assert any(event["type"] == "run.completed" for event in events)
        user_scope, project_scope = app.memory.repository.scopes_for(app.workspace)
        assert len((await app.memory.repository.aread(user_scope)).pending) == 1
        assert len((await app.memory.repository.aread(project_scope)).pending) == 1
    finally:
        await app.memory.learning.tasks.cancel()
        await app.aclose()


async def test_approval_resume_does_not_learn_before_tool_execution(tmp_path: Path) -> None:
    model = ContractModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"path": "approved.txt", "content": "approved"},
                        "id": "memory-write-1",
                    }
                ],
            ),
            AIMessage(content="完成"),
        ]
    )
    app = await contract_app(tmp_path, model)
    try:
        await app.memory.settings({"enabled": True, "learn": "auto", "idle_seconds": 300})
        first = await app.run("写入 approved.txt")
        assert first["status"] == "paused"
        assert (await app.memory.status())["pending"] == 0
        assert not (app.workspace / "approved.txt").exists()
        resumed = await app.command(
            "approve",
            {
                "thread_id": app.session_id,
                "decisions": [{"type": "approve"}],
                "grants": [],
            },
        )
        assert resumed["status"] == "completed"
        assert (app.workspace / "approved.txt").read_text(encoding="utf-8") == "approved"
        user_scope, project_scope = app.memory.repository.scopes_for(app.workspace)
        assert len((await app.memory.repository.aread(user_scope)).pending) == 1
        assert len((await app.memory.repository.aread(project_scope)).pending) == 1
    finally:
        await app.memory.learning.tasks.cancel()
        await app.aclose()


async def test_headless_flush_only_processes_its_own_source(tmp_path: Path) -> None:
    app = await contract_app(tmp_path, ContractModel())
    try:
        app.memory.learning._learner_factory = lambda _model: PreferenceLearner()
        await app.memory.settings({"enabled": True, "learn": "auto", "idle_seconds": 300})
        user_scope, _ = app.memory.repository.scopes_for(app.workspace)
        other = MemorySource(
            ref="turn:older:message", kind="user", order="2020-01-01T00:00:00+00:00",
            project_id=app.memory._scopes()[1].identity,
        )
        await app.memory.repository.aenqueue(user_scope, other)
        assert (await app.run("以后注释用中文", input_format="headless"))["status"] == "completed"
        await app.memory.flush_headless(app.session_id)
        snapshot = await app.memory.repository.aread(user_scope)
        assert [job.source.ref for job in snapshot.pending.values()] == [other.ref]
        assert len(await app.memory.list()) == 1
    finally:
        await app.memory.learning.tasks.cancel()
        await app.aclose()


async def test_memory_profile_is_bound_to_source_not_current_default(tmp_path: Path) -> None:
    app = await contract_app(tmp_path, ContractModel())
    try:
        app.config.profiles["memory"] = replace(
            app.config.profiles["test"], name="memory", api_key="separate-secret"
        )
        await app._save_config()
        await app.memory.settings(
            {"enabled": True, "learn": "auto", "model_profile": "memory", "idle_seconds": 300}
        )
        assert (await app.run("记住一个偏好", input_format="headless"))["status"] == "completed"
        user_scope, _ = app.memory.repository.scopes_for(app.workspace)
        pending = list((await app.memory.repository.aread(user_scope)).pending.values())
        assert len(pending) == 1
        assert pending[0].source.profile_name == "memory"
        assert pending[0].source.model_identity_sha256
        app.config.profiles["memory"] = replace(app.config.profiles["memory"], api_key="changed")
        assert app.memory.learning._profile_for_source(pending[0].source) is None
    finally:
        await app.memory.learning.tasks.cancel()
        await app.aclose()


async def test_real_langmem_pipeline_reaches_a_new_session(tmp_path: Path) -> None:
    app = await contract_app(tmp_path, ContractModel(), session_id="source-thread")
    memory_model = SourceAwareMemoryModel()
    try:
        app.memory.learning._learner_factory = lambda _model: MemoryLearner(memory_model)
        await app.memory.settings({"enabled": True, "learn": "auto", "idle_seconds": 300})
        result = await app.run("以后代码注释使用中文", input_format="headless")
        assert result["status"] == "completed"
        await app.memory.flush_headless(app.session_id)
        assert memory_model.calls == 2
        assert (await app.memory.list(scope="user"))[0]["text"] == "以后代码注释使用中文"
        audit = await app.audit.list(thread_id=app.session_id)
        memory_runs = [
            row for row in audit if str(row.get("task_id") or "").startswith("memory:")
        ]
        completed = [row for row in memory_runs if row["event"] == "model.completed"]
        assert len(completed) == 2
        assert all(row["details"]["usage"]["total_tokens"] == 19 for row in completed)
        assert "以后代码注释使用中文" not in json.dumps(memory_runs, ensure_ascii=False)
    finally:
        await app.aclose()

    next_model = ContractModel()
    next_app = await contract_app(tmp_path, next_model, session_id="next-thread")
    try:
        assert (await next_app.run("实现一个函数"))["status"] == "completed"
        assert any(
            "以后代码注释使用中文" in str(message.content)
            for message in next_model.received[0][1:]
        )
    finally:
        await next_app.aclose()


async def test_restart_recovers_every_completed_checkpoint_even_if_thread_status_is_running(
    tmp_path: Path,
) -> None:
    first = await contract_app(tmp_path, ContractModel(), session_id="recovery-thread")
    try:
        await first.memory.settings({"enabled": True, "learn": "auto", "idle_seconds": 300})
        handle, context = await first._context_for_thread(first.session_id)
        await first.runtime.invoke(handle, context, "第一轮", thread_id=first.session_id)
        await first.runtime.invoke(handle, context, "第二轮", thread_id=first.session_id)
        await first.runtime.set_thread_status(first.session_id, "running")
        assert (await first.memory.status())["pending"] == 0
    finally:
        await first.aclose()

    second = await contract_app(tmp_path, ContractModel(), session_id="recovery-thread")
    try:
        user_scope, project_scope = second.memory.repository.scopes_for(second.workspace)
        user_sources = {
            job.source.ref for job in (await second.memory.repository.aread(user_scope)).pending.values()
        }
        project_sources = {
            job.source.ref for job in (await second.memory.repository.aread(project_scope)).pending.values()
        }
        assert len(user_sources) == len(project_sources) == 2
        assert user_sources == project_sources
    finally:
        await second.memory.learning.tasks.cancel()
        await second.aclose()


async def test_read_file_observation_is_candidate_and_keeps_the_read_time_digest(
    tmp_path: Path,
) -> None:
    import hashlib

    model = ContractModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"path": "GUIDE.md"}, "id": "file-read-1"}
                ],
            ),
            AIMessage(content="已阅读"),
        ]
    )
    app = await contract_app(tmp_path, model)
    target = app.workspace / "GUIDE.md"
    target.write_text("原始内容", encoding="utf-8")
    old_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    try:
        app.memory.learning._learner_factory = lambda _model: ChangingFileLearner(
            target, change_file=True
        )
        await app.memory.settings({"enabled": True, "learn": "auto", "idle_seconds": 300})
        assert (await app.run("阅读 GUIDE.md", input_format="headless"))["status"] == "completed"
        await app.memory.flush_headless(app.session_id)
        records = await app.memory.list(scope="project")
        assert len(records) == 1
        assert records[0]["state"] == "needs_verification"
        assert records[0]["applicability"]["sha256"] == old_digest
        assert (await app.memory.refresh(records[0]["id"]))["confirmed"] == []
    finally:
        await app.aclose()


async def test_unchanged_file_candidate_can_be_refreshed_into_active_project_memory(
    tmp_path: Path,
) -> None:
    model = ContractModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"path": "GUIDE.md"}, "id": "read-current"}
                ],
            ),
            AIMessage(content="已阅读"),
        ]
    )
    app = await contract_app(tmp_path, model)
    target = app.workspace / "GUIDE.md"
    target.write_text("构建说明", encoding="utf-8")
    try:
        app.memory.learning._learner_factory = lambda _model: ChangingFileLearner(
            target, change_file=False
        )
        await app.memory.settings({"enabled": True, "learn": "auto", "idle_seconds": 300})
        assert (await app.run("阅读 GUIDE.md", input_format="headless"))["status"] == "completed"
        await app.memory.flush_headless(app.session_id)
        candidate = (await app.memory.list(scope="project"))[0]
        assert candidate["state"] == "candidate"
        assert (await app.memory.refresh(candidate["id"]))["confirmed"] == [candidate["id"]]
        active = await app.memory.get(candidate["id"])
        assert active["state"] == "active"
        assert active["review_after"] is not None
        matches = await app.memory.retriever.search(
            app.memory.repository.scopes_for(app.workspace), app.workspace, query="项目说明"
        )
        assert any(item.record.id == candidate["id"] for item in matches)
        target.write_text("更新后的构建说明", encoding="utf-8")
        changed = (await app.memory.list(scope="project"))[0]
        assert changed["state"] == "active"
        assert changed["effective_state"] == "needs_verification"
        assert "当前代码" in changed["validity_reason"]
        assert (await app.memory.status())["counts"]["needs_verification"] == 1
    finally:
        await app.aclose()


async def test_switching_thread_to_read_only_invalidates_inflight_learning(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class PausingLearner:
        async def extract(
            self, messages, existing, *, scope_kind, source_ids, verified_source_ids=(), callbacks=()
        ):
            started.set()
            await release.wait()
            return [
                MemoryChange(
                    action="insert",
                    subject="注释语言",
                    text="注释使用中文",
                    evidence_refs=(source_ids[0],),
                )
            ]

    app = await contract_app(tmp_path, ContractModel())
    try:
        app.memory.learning._learner_factory = lambda _model: PausingLearner()
        await app.memory.settings({"enabled": True, "learn": "auto", "idle_seconds": 0})
        assert (await app.run("以后注释使用中文"))["status"] == "completed"
        await asyncio.wait_for(started.wait(), timeout=5)
        await app.command("trust", "read_only")
        release.set()
        await app.memory.drain(5)
        assert await app.memory.list() == []
        assert (await app.memory.status())["pending"] == 0
    finally:
        release.set()
        await app.aclose()


async def test_other_cli_disabling_memory_blocks_inflight_commit(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class PausingLearner:
        async def extract(self, messages, existing, *, source_ids, callbacks=None, **kwargs):
            started.set()
            await release.wait()
            return [
                MemoryChange(
                    action="insert",
                    subject="注释语言",
                    text="注释使用中文",
                    evidence_refs=(source_ids[0],),
                )
            ]

    app = await contract_app(tmp_path, ContractModel())
    try:
        app.memory.learning._learner_factory = lambda _model: PausingLearner()
        await app.memory.settings({"enabled": True, "learn": "auto", "idle_seconds": 0})
        assert (await app.run("以后注释使用中文"))["status"] == "completed"
        await asyncio.wait_for(started.wait(), timeout=5)
        other = ConfigRepository(app.paths.home)
        latest = await other.load()
        latest.memory.enabled = False
        await other.save(latest)
        release.set()
        await app.memory.drain(5)
        assert await app.memory.list() == []
        assert (await app.memory.status())["pending"] == 0
    finally:
        release.set()
        await app.aclose()


async def test_explicit_disable_wins_even_when_local_config_was_already_disabled(
    tmp_path: Path,
) -> None:
    app = await contract_app(tmp_path, ContractModel())
    try:
        assert app.config.memory.enabled is False
        other = ConfigRepository(app.paths.home)
        remote = await other.load()
        remote.memory.enabled = True
        await other.save(remote)
        assert (await other.load()).memory.enabled is True
        await app.memory.settings({"enabled": False})
        assert (await other.load()).memory.enabled is False
        assert app.config.memory.enabled is False
    finally:
        await app.aclose()


async def test_session_memory_overrides_do_not_change_user_defaults(tmp_path: Path) -> None:
    app = await contract_app(tmp_path, ContractModel(), session_id="override-thread")
    try:
        await app.memory.settings({"enabled": True, "use": True, "learn": "auto", "idle_seconds": 300})
        await app.memory.remember("注释请用中文")
        selected = await app.memory.session_settings(
            app.session_id, {"use": False, "learn": "off"}
        )
        assert selected["use"] is False and selected["learn"] == "off"
        model = app.model_override
        assert (await app.run("检查项目"))["status"] == "completed"
        assert not any("注释请用中文" in str(message.content) for message in model.received[0])
        assert (await app.memory.status())["pending"] == 0
        new_thread = await app._new_session()
        inherited = await app.memory.session_settings(new_thread)
        assert inherited["use"] is True and inherited["learn"] == "auto"
    finally:
        await app.aclose()

    reopened = await contract_app(tmp_path, ContractModel(), session_id="override-thread")
    try:
        selected = await reopened.memory.session_settings(reopened.session_id)
        assert selected["use"] is False and selected["learn"] == "off"
    finally:
        await reopened.aclose()


async def test_openai_request_presents_memory_as_paired_tool_data(tmp_path: Path) -> None:
    reply = completion({"role": "assistant", "content": "ok"})
    async with mock_provider([httpx.Response(200, json=reply)]) as (model, requests):
        app = await contract_app(tmp_path, model)
        try:
            await app.memory.remember("历史说明：代码注释使用中文")
            assert (await app.run("检查文件"))["status"] == "completed"
            body = json.loads(requests[0].content)
            messages = body["messages"]
            assert "历史说明" not in str(messages[0]["content"])
            read_call = next(
                message for message in messages if message.get("tool_calls")
                and message["tool_calls"][0]["function"]["name"] == "search_memory"
            )
            tool_message = next(message for message in messages if message["role"] == "tool")
            assert tool_message["tool_call_id"] == read_call["tool_calls"][0]["id"]
            assert "历史说明" in tool_message["content"]
            assert messages[-1]["role"] == "user"
            references = await app.memory.recent(app.session_id)
            assert len(references) == 1
            assert references[0]["provided_to_model"] is True
            assert "用户范围内的有效偏好" in references[0]["match_reason"]
        finally:
            await app.aclose()


async def test_disabling_learning_revokes_pending_and_crash_gap_sources(tmp_path: Path) -> None:
    app = await contract_app(tmp_path, ContractModel(), session_id="revoked-thread")
    try:
        await app.memory.settings({"enabled": True, "learn": "auto", "idle_seconds": 300})
        assert (await app.run("请记住第一条", input_format="headless"))["status"] == "completed"
        assert (await app.memory.status())["pending"] == 2
        handle, context = await app._context_for_thread(app.session_id)
        await app.runtime.invoke(handle, context, "第二条未安排来源", thread_id=app.session_id)
        await app.memory.settings({"learn": "off"})
        assert (await app.memory.status())["pending"] == 0
        await app.memory.settings({"learn": "auto"})
        await app.memory.resume_pending()
        assert (await app.memory.status())["pending"] == 0
    finally:
        await app.memory.learning.tasks.cancel()
        await app.aclose()


async def test_headless_timeout_preserves_source_and_reports_deferred(tmp_path: Path) -> None:
    class StalledLearner:
        async def extract(self, *args, **kwargs):
            await asyncio.Event().wait()

    app = await contract_app(tmp_path, ContractModel())
    try:
        app.memory.learning._learner_factory = lambda _model: StalledLearner()
        await app.memory.settings(
            {
                "enabled": True,
                "learn": "auto",
                "idle_seconds": 300,
                "headless_timeout_seconds": 0.05,
            }
        )
        assert (await app.run("继续工作", input_format="headless"))["status"] == "completed"
        await app.memory.flush_headless(app.session_id)
        assert (await app.memory.status())["pending"] == 2
        assert sum(
            event["type"] == "memory.deferred" for event in app.drain_notifications()
        ) == 2
    finally:
        await app.aclose()


async def test_failed_model_turn_can_still_learn_explicit_user_preference(tmp_path: Path) -> None:
    class FailingMainModel(ContractModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("simulated model failure")

    app = await contract_app(tmp_path, FailingMainModel())
    try:
        app.memory.learning._learner_factory = lambda _model: PreferenceLearner()
        await app.memory.settings({"enabled": True, "learn": "auto", "idle_seconds": 300})
        result = await app.run("以后代码注释使用中文", input_format="headless")
        assert result["status"] == "failed"
        assert (await app.memory.status())["pending"] == 2
        await app.memory.flush_headless(app.session_id)
        assert (await app.memory.list(scope="user"))[0]["text"] == "用户要求代码注释使用中文"
    finally:
        await app.aclose()


async def test_session_learning_pause_revokes_only_its_thread(tmp_path: Path) -> None:
    app = await contract_app(tmp_path, ContractModel(), session_id="paused-thread")
    try:
        await app.memory.settings({"enabled": True, "learn": "auto", "idle_seconds": 300})
        assert (await app.run("本会话来源", input_format="headless"))["status"] == "completed"
        await app.memory.session_settings(app.session_id, {"learn": "off"})
        assert (await app.memory.status())["pending"] == 0
        await app.memory.session_settings(app.session_id, {"learn": "auto"})
        await app.memory.resume_pending()
        assert (await app.memory.status())["pending"] == 0
    finally:
        await app.memory.learning.tasks.cancel()
        await app.aclose()
