# batch_executor 缺口上半：分区与执行。

from types import SimpleNamespace

import lib.tools.batch_executor as be
from lib.tools.batch_executor import ToolBatchExecutor


def _meta(safe=True, abort=False):
    return SimpleNamespace(check_concurrency_safe=lambda args: safe, can_abort_siblings=abort)


def _req(name, args=None, cid="c1"):
    from lib.tools.batch_executor import ToolCallRequest
    return ToolCallRequest(tool_name=name, arguments=args or {}, tool_call_id=cid)


class TestExecute:
    def test_concurrent_abort(self, monkeypatch):
        monkeypatch.setattr(be, "get_tool_meta", lambda name: _meta(safe=True, abort=True))

        def fail(**k):
            raise RuntimeError("boom")

        ex = ToolBatchExecutor({"a": fail, "b": lambda **k: "ok"})
        out = ex.execute_batch([_req("a"), _req("b")])
        assert out.has_aborted is True
        assert "sibling_error" in out.abort_reason

    def test_serial_abort_then_skip(self, monkeypatch):
        monkeypatch.setattr(be, "get_tool_meta", lambda name: _meta(safe=False, abort=True))
        ex = ToolBatchExecutor({"a": lambda **k: (_ for _ in ()).throw(RuntimeError("boom")), "b": lambda **k: "ok"})
        out = ex.execute_batch([_req("a"), _req("b", cid="c2")])
        assert out.has_aborted is True
        assert "已中止" in out.results[1].error

    def test_unknown_tool(self):
        ex = ToolBatchExecutor({})
        out = ex.execute_batch([_req("ghost")])
        assert "未知工具" in out.results[0].error

    def test_abort_signal_styles(self):
        ex = ToolBatchExecutor({"a": lambda **k: "ok"}, abort_signal=SimpleNamespace(is_aborted=True, reason="user"))
        assert "已中止" in ex.execute_batch([_req("a")]).results[0].error
        ex = ToolBatchExecutor({"a": lambda **k: "ok"}, abort_signal=SimpleNamespace(aborted=True))
        assert "已中止" in ex.execute_batch([_req("a")]).results[0].error
        ex = ToolBatchExecutor({"a": lambda **k: "ok"}, abort_signal=SimpleNamespace())
        assert ex.execute_batch([_req("a")]).results[0].result == "ok"

    def test_future_crash(self, monkeypatch):
        monkeypatch.setattr(be, "get_tool_meta", lambda name: _meta(safe=True))
        ex = ToolBatchExecutor({"a": lambda **k: "ok", "b": lambda **k: "ok"})
        monkeypatch.setattr(ex, "_execute_one",
                            lambda req: (_ for _ in ()).throw(RuntimeError("lost")))
        out = ex.execute_batch([_req("a"), _req("b", cid="c2")])
        assert all(r.is_error for r in out.results)


class TestToolWrapper:
    def test_skips_unnamed(self):
        tool = be.create_batch_execute_tool([SimpleNamespace(), SimpleNamespace(name="batch_execute")])
        assert tool.name == "batch_execute"

    def test_too_many(self):
        import pytest as _pytest

        tool = be.create_batch_execute_tool([])
        with _pytest.raises(ValueError):
            tool.invoke({"calls": [{"tool_name": "x", "arguments": {}}] * 1000})

    def test_dict_calls(self):
        from lib.tools.file_tools import read_file

        tool = be.create_batch_execute_tool([read_file])
        out = tool.invoke({"calls": [{"tool_name": "read_file", "arguments": {"path": "nope-xyz"}}]})
        assert "read_file" in out


class TestEventsGap:
    def test_skip_non_ai(self):
        from langchain_core.messages import HumanMessage
        from lib.runtime.events import extract_public_tool_events

        assert extract_public_tool_events(HumanMessage(content="hi")) == []

    def test_kwargs_tool_calls(self):
        from langchain_core.messages import AIMessage
        from lib.runtime.events import extract_public_tool_events

        msg = AIMessage(content="", additional_kwargs={"tool_calls": [{"name": "t", "args": {}, "id": "1"}]})
        assert extract_public_tool_events(msg)[0]["tool_name"] == "t"

    def test_collect_none_seen(self):
        from lib.runtime.events import _collect_messages, extract_public_tool_events

        out: list = []
        _collect_messages(None, out, seen=set())
        x = [1]
        seen: set = set()
        _collect_messages(x, out, seen)
        _collect_messages(x, out, seen)
        assert out == []
        assert extract_public_tool_events({"type": "ai", "content": "x"}) == []

    def test_call_function_fallback(self):
        from lib.runtime.events import _tool_call_event

        assert _tool_call_event({"function": {"name": "f", "arguments": {"a": 1}}})["tool_name"] == "f"
        assert _tool_call_event({}) is None
        assert _tool_call_event({"name": "", "function": {}}) is None

    def test_normalize_args(self):
        from lib.runtime.events import _normalize_arguments, _normalize_result

        assert _normalize_arguments('{"a": 1}') == {"a": 1}
        assert _normalize_arguments("{bad") == {"raw": "{bad"}
        assert _normalize_arguments(None) == {}
        assert _normalize_result("  {bad") == "  {bad"
        assert _normalize_result('{"a": 1}') == {"a": 1}


class TestSymbolsGap:
    def test_props(self, tmp_path):
        from lib.core.symbols import CodeSymbol

        s = CodeSymbol(name="f", kind="function", path="a.py", line=1, column=0, parent="", signature="f()")
        assert s.qualified_name == "f"
        assert s.to_dict()["qualified_name"] == "f"
        s2 = CodeSymbol(name="m", kind="method", path="a.py", line=2, column=0, parent="C", signature="m()")
        assert s2.qualified_name == "C.m"

    def test_scan_shapes(self, tmp_path, monkeypatch):
        import lib.core.symbols as _sym
        from lib.core.symbols import SymbolIndex

        (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("def bar(:\n", encoding="utf-8")
        (tmp_path / "c.js").write_text("export class Foo {}\nconst f = (a, b) => a;\nconst g = x => x;\nfunction h(a) {}\n", encoding="utf-8")
        (tmp_path / "big.py").write_text("x" * 100, encoding="utf-8")
        assert SymbolIndex(tmp_path / "nope").scan() == []
        assert SymbolIndex(tmp_path / "a.py").scan() == []
        monkeypatch.setattr(_sym, "MAX_SYMBOL_FILES", 1)
        assert len(SymbolIndex(tmp_path).scan()) <= 2
        monkeypatch.setattr(_sym, "MAX_SYMBOL_FILES", 100000)
        monkeypatch.setattr(_sym, "MAX_SYMBOL_FILE_SIZE", 10)
        names = [s.name for s in SymbolIndex(tmp_path).scan()]
        assert "foo" not in names

    def test_stat_fails(self, tmp_path, monkeypatch):
        import errno as _errno
        from pathlib import Path as _Path
        from lib.core.symbols import SymbolIndex

        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        real_stat = _Path.stat

        def flaky(self, *args, **kwargs):
            if self.name == "a.py":
                raise OSError(_errno.ENOENT, "gone")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(_Path, "stat", flaky)
        assert SymbolIndex(tmp_path).scan() == []

    def test_search_limit(self, tmp_path):
        (tmp_path / "a.py").write_text("def foo():\n    pass\ndef food():\n    pass\nclass C:\n    def m(self):\n        pass\n", encoding="utf-8")
        from lib.core.symbols import SymbolIndex

        idx = SymbolIndex(tmp_path)
        assert len(idx.search(query="foo", limit=1)) == 1
        assert idx.find("") == []
        assert [s.name for s in idx.find("C.m")] == ["m"]
        assert [s.name for s in idx.find("m")] == ["m"]
        assert idx.find("oo") != []

    def test_render_empty(self, tmp_path):
        from lib.core.symbols import index_project_symbols, render_symbols, summarize_symbol_index

        assert render_symbols([]) != ""
        (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
        assert "Total" in summarize_symbol_index(tmp_path)
        assert index_project_symbols(tmp_path) != []

    def test_unreadable(self, tmp_path, monkeypatch):
        from pathlib import Path as _Path
        from lib.core.symbols import _extract_symbols

        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        real_read = _Path.read_text

        def flaky(self, *args, **kwargs):
            if self.name == "a.py":
                raise OSError("busy")
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(_Path, "read_text", flaky)
        assert _extract_symbols(tmp_path / "a.py", tmp_path) == []

    def test_python_shapes(self, tmp_path):
        from lib.core.symbols import _extract_symbols

        (tmp_path / "a.py").write_text("class C:\n    def m(self, *a, k, **kw):\n        pass\n    async def n(self):\n        pass\ndef outer():\n    def inner():\n        pass\n", encoding="utf-8")
        syms = _extract_symbols(tmp_path / "a.py", tmp_path)
        kinds = {s.kind for s in syms}
        assert {"class", "method", "function"} <= kinds
        assert any("*a" in s.signature for s in syms if s.name == "m")
        assert any(s.signature.startswith("async") for s in syms if s.name == "n")

    def test_ignore_relative(self, tmp_path):
        from lib.core.symbols import _relative_path, _should_ignore

        assert _should_ignore(tmp_path / "x" / "node_modules" / "y", tmp_path) is True
        assert _should_ignore(tmp_path / "a.py", tmp_path) is False
        assert _relative_path(tmp_path / "a.py", tmp_path) == "a.py"
        assert _relative_path(__import__("pathlib").Path("/outside/x"), tmp_path).endswith("x")


class TestPathsGap:
    def test_session_id_errors(self):
        import pytest as _pytest
        from lib.core.paths import _session_dir_name

        for bad in ("", "..", "a/b", "a\\b", "/abs", "C:/x", "bad!"):
            with _pytest.raises(ValueError):
                _session_dir_name(bad)
        assert _session_dir_name("abc-123") == "abc-123"

    def test_props(self, tmp_path):
        from lib.core.paths import SayacodePaths

        paths = SayacodePaths.resolve(home=str(tmp_path))
        assert paths.hook_trusted_projects.name == "trusted_projects.json"
        assert paths.mcp_trusted_projects.name == "mcp_trusted_projects.json"
        assert paths.user_memory.name == "memory.md"
        assert paths.project_hooks(tmp_path).name == "hooks.json"

    def test_config_store(self, tmp_path):
        from lib.core.paths import ConfigStore, StateStore

        store = ConfigStore()
        assert store.read_json(tmp_path / "nope.json", default="d") == "d"
        (tmp_path / "bad.json").write_text("{broken", encoding="utf-8")
        assert store.read_json(tmp_path / "bad.json", default="d") == "d"
        (tmp_path / "ok.json").write_text('{"a": 1}', encoding="utf-8")
        assert store.read_json(tmp_path / "ok.json") == {"a": 1}
        assert store.write_json(tmp_path / "w.json", {"a": 1}).exists()
        st = StateStore()
        assert st.write_text(tmp_path / "t.txt", "hi").exists()
        assert st.write_json(tmp_path / "j.json", {}).exists()
        assert st.workspace_state_dir(tmp_path).name != ""


class TestSearchGap:
    def test_details_skip(self):
        from types import SimpleNamespace
        from lib.tools.tool_search import _tool_details

        assert _tool_details([SimpleNamespace()]) == {}
        assert _tool_details([SimpleNamespace(name="t", description="d", get_input_schema=lambda: (_ for _ in ()).throw(RuntimeError("boom")))])["t"]["input_schema"] == {}

    def test_deferred_unknown(self):
        from lib.tools.tool_search import create_deferred_tool_invoke_tool

        tool = create_deferred_tool_invoke_tool([])
        import pytest as _pytest

        with _pytest.raises(ValueError):
            tool.invoke({"tool_name": "ghost", "arguments": {}})


class TestRegistryGap:
    def test_dedup_bind(self):
        from types import SimpleNamespace
        from lib.tools.registry import _bind_tool_to_context, _deduplicate_tools

        t = SimpleNamespace(name="a")
        assert _deduplicate_tools([t, t, SimpleNamespace()]) == [t]
        assert _bind_tool_to_context(SimpleNamespace(), None) is not None

    def test_meta_none_registers(self):
        from langchain_core.tools import tool as _tool
        from lib.tools.registry import ToolRegistry, get_tool_meta
        from lib.runtime import RuntimeContext

        @_tool
        def my_unique_xyz_tool(arg: str) -> str:
            """Unique tool."""
            return arg

        ctx = RuntimeContext(workspace="/tmp", model_type="o", model_name="m", model_config={})
        reg = ToolRegistry(ctx)
        reg.compose_tools([my_unique_xyz_tool])
        assert get_tool_meta("my_unique_xyz_tool") is not None


class TestLaunchGap:
    def test_overrides(self):
        import pytest as _pytest
        from lib.runtime.launch_config import LaunchModelOverrides

        assert LaunchModelOverrides().has_model_overrides is False
        assert LaunchModelOverrides(model_name="m").has_model_overrides is True
        assert LaunchModelOverrides(context_window=None).parsed_context_window() is None
        assert LaunchModelOverrides(context_window="8k").parsed_context_window() == 8192
        with _pytest.raises(ValueError):
            LaunchModelOverrides(context_window="oops").parsed_context_window()

    def test_configure_path(self, tmp_path):
        from lib.api_config import APIConfigManager
        from lib.runtime.launch_config import LaunchModelOverrides, ModelLaunchResolver

        mgr = APIConfigManager(config_dir=str(tmp_path / "cfg"))
        seen = {}
        resolver = ModelLaunchResolver(
            api_manager=mgr,
            configure_model=lambda **kw: ("ollama", "m", {"context_window": 8000}),
            ensure_context_window=lambda *a, **k: 8000,
            interactive_input=False,
            on_profile_saved=lambda name: seen.update(saved=name),
        )
        out = resolver.resolve(LaunchModelOverrides())
        assert out.model_type == "ollama" and seen.get("saved") is not None

    def test_configure_fail(self, tmp_path):
        from lib.api_config import APIConfigManager
        from lib.runtime.launch_config import LaunchModelOverrides, ModelLaunchResolver

        mgr = APIConfigManager(config_dir=str(tmp_path / "cfg"))
        seen = {}
        resolver = ModelLaunchResolver(
            api_manager=mgr,
            configure_model=lambda **kw: ("ollama", "m", {}),
            ensure_context_window=lambda *a, **k: 8000,
            interactive_input=False,
            on_profile_not_saved=lambda: seen.update(failed=True),
        )
        import lib.runtime.launch_config as _lc
        real_save = _lc.save_model_profile
        _lc.save_model_profile = lambda *a, **k: None
        try:
            out = resolver.resolve(LaunchModelOverrides())
        finally:
            _lc.save_model_profile = real_save
        assert out.model_type == "ollama" and seen.get("failed") is True

    def test_redact_list(self):
        from lib.runtime.events import _redact_public_value

        assert _redact_public_value(["a", "b"]) == ["a", "b"]
        assert _redact_public_value(42) == 42


class TestPlansGap:
    def test_from_dict_robust(self, tmp_path):
        from lib.core.plans import Plan

        plan = Plan.from_dict({"goal": "g", "tasks": ["oops", {"id": "t1", "title": "t", "status": "bogus"}], "rounds": 2})
        assert len(plan.tasks) == 1 and plan.tasks[0].status == "todo" and plan.rounds == 2
        assert Plan.from_dict({}).goal == ""

    def test_for_runtime_no_session(self, tmp_path):
        from types import SimpleNamespace
        from lib.core.plans import PlanStore

        store = PlanStore.for_runtime(SimpleNamespace(workspace=tmp_path, session=None))
        assert store.session_id == "default"
        store2 = PlanStore.for_runtime(SimpleNamespace(workspace=tmp_path, session=SimpleNamespace()))
        assert store2.session_id == "default"

    def test_save_crash(self, tmp_path, monkeypatch):
        from pathlib import Path as _Path
        from lib.core.plans import PlanStore

        store = PlanStore(tmp_path, "s")
        monkeypatch.setattr(_Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
        plan = store.create("g", ["t"])
        assert plan.goal == "g"

    def test_create_errors(self, tmp_path):
        from lib.core.plans import PlanStore

        store = PlanStore(tmp_path, "s")
        with __import__("pytest").raises(ValueError):
            store.create("", ["t"])
        with __import__("pytest").raises(ValueError):
            store.create("g", [])
        with __import__("pytest").raises(ValueError):
            store.create("g", [f"t{i}" for i in range(21)])

    def test_get_corrupt(self, tmp_path):
        from lib.core.plans import PlanStore

        store = PlanStore(tmp_path, "s")
        path = store._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        assert store.get() is None
        assert store.note_round() == 0
        with __import__("pytest").raises(ValueError):
            store.update("t1", "done")

    def test_update_missing_task(self, tmp_path):
        from lib.core.plans import PlanStore

        store = PlanStore(tmp_path, "s")
        store.create("g", ["t"])
        with __import__("pytest").raises(ValueError):
            store.update("ghost", "done")


class TestPlanGraphGap:
    def test_missing_pieces(self, tmp_path, monkeypatch):
        import lib.core.plan_graph as _pg
        from lib.core.plans import PlanStore

        store = PlanStore(tmp_path, "s")
        monkeypatch.setattr(_pg, "StateGraph", None)
        with __import__("pytest").raises(RuntimeError):
            _pg.build_plan_graph(model=None, plan_tools=[], run_turn=lambda p: "", store=store, overlay="", checkpointer=object())
        monkeypatch.setattr(_pg, "create_langchain_agent", None)
        with __import__("pytest").raises(RuntimeError):
            _pg.build_plan_graph(model=None, plan_tools=[], run_turn=lambda p: "", store=store, overlay="", checkpointer=object())
        with __import__("pytest").raises(RuntimeError):
            _pg.build_plan_graph(model=None, plan_tools=[], run_turn=lambda p: "", store=store, overlay="", checkpointer=None)

    def test_planner_crash(self, tmp_path):
        from langchain_core.language_models.chat_models import BaseChatModel
        import lib.core.plan_graph as _pg
        from lib.core.plans import PlanStore

        class DownModel(BaseChatModel):
            @property
            def _llm_type(self):
                return "down"

            def _generate(self, messages, stop=None, run_manager=None, **kw):
                raise ConnectionError("down")

            def bind_tools(self, tools, **kw):
                return self

        store = PlanStore(tmp_path, "s")
        saver, conn = _pg.open_plan_checkpointer(tmp_path)
        try:
            graph = _pg.build_plan_graph(model=DownModel(), plan_tools=[], run_turn=lambda p: "", store=store, overlay="", checkpointer=saver)
            out = graph.invoke({"goal": "g", "max_rounds": 1}, {"configurable": {"thread_id": "t1"}})
            assert "规划失败" in str(out.get("final", ""))
        finally:
            conn.close()
