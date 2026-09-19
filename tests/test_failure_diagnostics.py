# 静默失败诊断：这些路径出错时必须有可见记录，不能无声吞掉。

import logging
import pytest

from lib.runtime import session_store as ss


class TestMemoryRestoreDiagnostics:
    def test_unreadable_memory_is_logged(self, tmp_path, caplog):
        # 会话文件缺失 + 旧记忆不可读：告警并返回新会话，不崩。
        paths = ss.workspace_session_paths(tmp_path, "diag-session")
        paths["memory"].parent.mkdir(parents=True, exist_ok=True)
        paths["memory"].mkdir()
        with caplog.at_level(logging.WARNING):
            _, _, restored = ss.load_session_memory_pair(tmp_path, "diag-session")
        assert "记忆恢复失败" in caplog.text
        assert restored is False

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
