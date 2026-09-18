# 静默失败诊断：这些路径出错时必须有可见记录，不能无声吞掉。

import logging
import pytest
from pathlib import Path
from types import SimpleNamespace

from lib.core.plans import PlanStore
from lib.runtime import session_store as ss


def _raise_oserror(*args, **kwargs):
    raise OSError("disk full")


class TestPlanPersistenceDiagnostics:
    def test_save_failure_is_logged(self, tmp_path, monkeypatch, caplog):
        store = PlanStore(tmp_path, session_id="diag")
        plan = store.create("目标", ["任务一"])
        monkeypatch.setattr(Path, "write_text", _raise_oserror)
        with caplog.at_level(logging.WARNING):
            store._save(plan)
        assert "计划落盘失败" in caplog.text

    def test_clear_failure_is_logged_at_debug(self, tmp_path, monkeypatch, caplog):
        store = PlanStore(tmp_path, session_id="diag")
        store.create("目标", ["任务一"])
        monkeypatch.setattr(Path, "unlink", lambda *a, **k: _raise_oserror())
        with caplog.at_level(logging.DEBUG):
            store.clear()
        assert "清理计划文件失败" in caplog.text


class TestMemoryRestoreDiagnostics:
    def test_unreadable_memory_is_logged(self, tmp_path, caplog):
        session, memory, _ = ss.load_session_memory_pair(tmp_path, "diag-session")
        memory.add_interaction("q", "a")
        ss.save_runtime_state(
            SimpleNamespace(workspace=tmp_path, session=session, memory=memory, context=None)
        )
        paths = ss.workspace_session_paths(tmp_path, session.session_id)
        paths["memory"].unlink()
        paths["memory"].mkdir()
        with caplog.at_level(logging.WARNING):
            ss.load_session_memory_pair(tmp_path, session.session_id)
        assert "记忆恢复失败" in caplog.text

class TestRunnerDestructorReleasesConnection:
    def test_dropping_runner_closes_checkpointer(self, tmp_path):
        """close() 的契约是「rebuild/析构时调用」；调用方漏掉时要靠析构兜底。"""
        import gc
        import sqlite3

        from lib.core.agent_runtime import AgentRunner

        runner = AgentRunner(
            model=object(),
            tools=[],
            system_prompt="p",
            checkpoint_path=str(tmp_path / "ckpt.sqlite3"),
        )
        assert runner._open_checkpointer() is not None
        raw = runner._saver_conn
        assert raw is not None
        del runner
        gc.collect()
        with pytest.raises(sqlite3.ProgrammingError):
            raw.execute("SELECT 1")
