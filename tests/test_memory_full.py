# core/memory.py 全覆盖：交互、查询、摘要、导入导出。

import json


from lib.core.memory import FileModification, Interaction, MemoryManager


def _mgr(**kw):
    # 固定会话号，避免时间戳断言抖动。
    kw.setdefault("session_id", "s1")
    return MemoryManager(**kw)


def _filled(n=2):
    m = _mgr()
    for i in range(n):
        m.record_tool_use("read_file", result=f"r{i}", modified_file=f"f{i}.py")
        m.add_interaction(f"q{i}", f"a{i}")
    return m


class TestRecord:
    def test_default_session(self):
        m = MemoryManager()
        assert m.session_id and m.max_history == 50

    def test_start_resets_buffers(self):
        m = _mgr()
        m.record_tool_use("t", result="r", modified_file="f")
        m.start_interaction()
        assert m._current_tools == [] and m._current_modified_files == []

    def test_tool_result_truncated(self):
        m = _mgr()
        m.record_tool_use("t", result="x" * 2000)
        assert m._current_tool_results[0].endswith("[结果已截断]")

    def test_tool_no_result(self):
        m = _mgr()
        m.record_tool_use("t")
        assert m._current_tool_results == []
        assert m.tool_usage == {"t": 1}

    def test_add_interaction_copies_buffers(self):
        m = _mgr()
        m.record_tool_use("t", result="r", modified_file="f.py")
        it = m.add_interaction("q", "a")
        assert isinstance(it, Interaction)
        assert it.tools_used == ["t"] and it.modified_files == ["f.py"]
        assert len(m.file_modifications) == 1
        assert m.file_modifications[0].action == "modified"

    def test_history_trimmed(self):
        m = _mgr(max_history=2)
        for i in range(3):
            m.add_interaction(f"q{i}", f"a{i}")
        assert [i.user_input for i in m.interactions] == ["q1", "q2"]

    def test_file_mods_trimmed(self):
        m = _mgr(max_file_records=2)
        for i in range(3):
            m.add_file_modification(f"f{i}", "created", details="d")
        assert [x.file_path for x in m.file_modifications] == ["f1", "f2"]

    def test_file_mod_defaults(self):
        f = FileModification(timestamp="t", file_path="p", action="created")
        assert f.details == ""


class TestQuery:
    def test_recent_empty(self):
        assert _mgr().get_recent_context() == "暂无对话历史"

    def test_recent_full(self):
        m = _mgr()
        m.record_tool_use("t", modified_file="f.py")
        m.add_interaction("q" * 150, "a" * 150)
        out = m.get_recent_context()
        assert "第 1 轮" in out and "工具" in out and "修改文件" in out
        assert "..." in out

    def test_recent_slice(self):
        m = _filled(5)
        out = m.get_recent_context(2)
        assert "最近 2 轮对话" in out

    def test_history_variants(self):
        m = _mgr()
        m.record_tool_use("t", result="res", modified_file="f.py")
        m.add_interaction("q", "a")
        full = m.get_interaction_history()[0]
        assert full["tools"] == ["t"] and "results" not in full
        assert full["modified_files"] == ["f.py"]
        bare = m.get_interaction_history(include_tools=False)[0]
        assert "tools" not in bare
        with_res = m.get_interaction_history(include_results=True)[0]
        assert with_res["results"] == ["res"]

    def test_modified_files_dedup(self):
        m = _mgr()
        m.record_tool_use("t", modified_file="b.py")
        m.add_interaction("q", "a")
        m.add_file_modification("a.py", "created")
        m.add_file_modification("b.py", "modified")
        assert m.get_modified_files() == ["a.py", "b.py"]

    def test_usage_copy(self):
        m = _filled()
        stats = m.get_tool_usage_stats()
        stats["read_file"] = 999
        assert m.tool_usage["read_file"] == 2

    def test_search_user(self):
        m = _filled()
        assert len(m.search_interactions("q1")) == 1

    def test_search_ai(self):
        m = _filled()
        assert len(m.search_interactions("a0")) == 1

    def test_search_tool(self):
        m = _filled()
        assert len(m.search_interactions("read_file")) == 2

    def test_search_case_sensitive(self):
        m = _mgr()
        m.add_interaction("Hello", "world")
        assert m.search_interactions("hello") != []
        assert m.search_interactions("hello", case_sensitive=True) == []
        assert m.search_interactions("Hello", case_sensitive=True) != []

    def test_search_no_match(self):
        assert _filled().search_interactions("zzz") == []


class TestSummarizeExport:
    def test_summarize_empty(self):
        assert "空记忆" in _mgr().summarize()

    def test_summarize_full(self):
        m = _mgr()
        for i in range(12):
            m.record_tool_use(f"tool{i % 3}", modified_file=f"f{i}.py")
            m.add_interaction("q" * 60, "a" * 60)
        out = m.summarize()
        assert "TOP5" in out and "还有 2 个文件" in out
        assert "最近活动" in out and "..." in out

    def test_export_roundtrip(self):
        m = _filled()
        data = json.loads(m.export_to_json())
        assert data["session_id"] == "s1"
        assert len(data["interactions"]) == 2
        m2 = _mgr(session_id="other")
        assert m2.load_from_json(m.export_to_json()) is True
        assert m2.session_id == "s1"
        assert len(m2.interactions) == 2
        assert len(m2.file_modifications) == 2

    def test_load_bad_json(self):
        assert _mgr().load_from_json("{broken") is False

    def test_load_missing_keys(self):
        m = _mgr()
        assert m.load_from_json("{}") is True
        assert m.interactions == [] and m.tool_usage == {}


class TestManage:
    def test_clear(self):
        m = _filled()
        m.clear()
        assert len(m) == 0 and m.get_stats()["total_tool_uses"] == 0

    def test_clear_old(self):
        m = _mgr()
        m.add_interaction("old", "a")
        m.interactions[0].timestamp = "2000-01-01T00:00:00"
        m.add_interaction("new", "b")
        removed = m.clear_old_interactions("2020-01-01T00:00:00")
        assert removed == 1 and len(m) == 1

    def test_stats(self):
        m = _filled()
        stats = m.get_stats()
        assert stats["total_interactions"] == 2
        assert stats["unique_tools_used"] == 1
        assert stats["unique_files_modified"] == 2

    def test_repr(self):
        assert "s1" in repr(_filled())
