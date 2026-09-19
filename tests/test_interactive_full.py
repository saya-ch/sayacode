# interactive 全覆盖：主循环、命令分发、回合执行、通知排空。

from types import SimpleNamespace


import lib.runtime.interactive as inter
from lib.runtime.interactive import InteractiveLoop
from lib.runtime.state import create_app_state


class FakeAgent:
    # 交互循环用的 agent 替身。
    def __init__(self, state):
        self.session = state.session
        self.memory = state.memory
        self.closed = False
        self.turns = []

    def stream_run(self, prompt):
        self.turns.append(prompt)
        return iter([])

    def close(self):
        self.closed = True


def _loop(tmp_path, inputs=None, **kw):
    # 带剧本输入的循环固件。

    state = create_app_state(tmp_path)
    agent = FakeAgent(state)
    loop = InteractiveLoop(agent=agent, state=state, user_config=None, **kw)
    return loop, agent, state


def _feed(monkeypatch, inputs):
    # 喂 console.input 剧本。
    from lib import theme as _theme

    seq = iter(inputs)
    monkeypatch.setattr(_theme.console, "input", lambda *a, **k: next(seq))


class TestRun:
    def test_exit_command(self, tmp_path, monkeypatch):
        loop, agent, _ = _loop(tmp_path)
        _feed(monkeypatch, ["/exit"])
        loop.run()
        assert agent.closed is True

    def test_empty_then_exit(self, tmp_path, monkeypatch):
        loop, agent, _ = _loop(tmp_path)
        _feed(monkeypatch, ["   ", "/exit"])
        loop.run()
        assert agent.turns == []

    def test_agent_turn(self, tmp_path, monkeypatch):
        loop, agent, _ = _loop(tmp_path)
        _feed(monkeypatch, ["hello", "/exit"])
        loop.run()
        assert agent.turns == ["hello"]

    def test_eof(self, tmp_path, monkeypatch):
        from lib import theme as _theme

        loop, agent, _ = _loop(tmp_path)
        monkeypatch.setattr(_theme.console, "input",
                            lambda *a, **k: (_ for _ in ()).throw(EOFError()))
        loop.run()
        assert agent.closed is True

    def test_keyboard_interrupt(self, tmp_path, monkeypatch):
        from lib import theme as _theme

        loop, agent, _ = _loop(tmp_path)
        monkeypatch.setattr(_theme.console, "input",
                            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
        loop.run()
        assert agent.closed is True

    def test_generic_error_continues(self, tmp_path, monkeypatch):
        loop, agent, _ = _loop(tmp_path)
        calls = iter([RuntimeError("boom"), "/exit"])

        def flaky(*a, **k):
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value

        from lib import theme as _theme
        monkeypatch.setattr(_theme.console, "input", flaky)
        loop.run()
        assert agent.closed is True

    def test_restored_message(self, tmp_path, monkeypatch):
        loop, agent, state = _loop(tmp_path)
        state.restored_session = True
        state.session.add_user_message("hi")
        _feed(monkeypatch, ["/exit"])
        loop.run()

    def test_start_blocked(self, tmp_path, monkeypatch):
        loop, agent, _ = _loop(tmp_path)
        monkeypatch.setattr(inter, "trigger_hook_event",
                            lambda name, payload: "blocked!" if name == "SessionStart" else "")
        _feed(monkeypatch, ["/exit"])
        loop.run()

    def test_no_close_method(self, tmp_path, monkeypatch):
        state = create_app_state(tmp_path)
        agent = SimpleNamespace(session=state.session, stream_run=lambda p: iter([]))
        loop = InteractiveLoop(agent=agent, state=state)
        _feed(monkeypatch, ["/exit"])
        loop.run()

    def test_mcp_attach(self, tmp_path, monkeypatch):
        mcp = SimpleNamespace(attached=None)
        mcp.attach_runtime = lambda rt: setattr(mcp, "attached", rt)
        loop, _, _ = _loop(tmp_path, mcp_service=mcp)
        _feed(monkeypatch, ["/exit"])
        loop.run()
        assert mcp.attached is not None


class TestTurn:
    def test_unknown_slash(self, tmp_path):
        loop, _, _ = _loop(tmp_path)
        loop._run_agent_turn("/ghost-cmd")

    def test_unknown_slash_suggestion(self, tmp_path):
        loop, _, _ = _loop(tmp_path, builtin_commands=["/help", "/history"])
        loop._run_agent_turn("/hep")

    def test_custom_command(self, tmp_path):
        d = tmp_path / ".claude" / "commands"
        d.mkdir(parents=True)
        (d / "greet.md").write_text("Hello $1", encoding="utf-8")
        loop, agent, _ = _loop(tmp_path)
        loop._run_agent_turn("/greet Bob")
        assert agent.turns == ["Hello Bob"]

    def test_prompt_blocked(self, tmp_path, monkeypatch):
        loop, agent, _ = _loop(tmp_path)
        monkeypatch.setattr(inter, "trigger_hook_event",
                            lambda name, payload: "nope" if payload.get("input") == "hi" else "")
        assert loop._prompt_blocked("hi") is True
        _feed(monkeypatch, ["hi", "/exit"])
        loop.run()
        assert agent.turns == []

    def test_suggest(self, tmp_path):
        loop, _, _ = _loop(tmp_path, builtin_commands=["/help"])
        assert loop._suggest_command_invocations("/hep") != []
        assert loop._suggest_command_invocations("") == []
        assert "/help" in loop._known_command_invocations()

    def test_known_broken_custom(self, tmp_path, monkeypatch):
        import lib.runtime.interactive as _inter

        loop, _, _ = _loop(tmp_path)
        monkeypatch.setattr(_inter, "list_custom_commands",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert "/x" not in loop._known_command_invocations()


class TestDispatchDrain:
    def test_dispatch_syncs(self, tmp_path):
        loop, _, _ = _loop(tmp_path, builtin_commands=["/help"])
        assert loop.dispatch_command("/help") is True
        assert loop.dispatch_command("/nope") is None

    def test_dispatch_ensure_store(self, tmp_path):
        loop, _, state = _loop(tmp_path, ensure_context_window=lambda *a: True)
        assert loop.dispatch_command("/help") is True

    def test_runtime_cached(self, tmp_path):
        loop, _, state = _loop(tmp_path)
        assert loop._runtime() is loop._runtime()

    def test_history_helpers(self, tmp_path):
        p = tmp_path / "hist"
        inter._append_history("line", p)
        assert p.read_text(encoding="utf-8") == "line\n"
        assert inter._append_history("x", None) is None
        assert inter._resolve_history_path().name == "history"

    def test_history_loaded_shortcircuit(self, monkeypatch):
        monkeypatch.setattr(inter, "_history_loaded", True)
        inter._setup_readline_history(__import__("pathlib").Path("/tmp/x"))
        monkeypatch.setattr(inter, "_history_loaded", False)

    def test_readline_with_apis(self, tmp_path, monkeypatch):
        import sys as _sys
        from types import ModuleType as _Module

        fake = _Module("readline")
        fake.read_history_file = lambda p: None
        written = []
        fake.write_history_file = lambda p: written.append(p)
        monkeypatch.setitem(_sys.modules, "readline", fake)
        monkeypatch.setattr(inter, "_history_loaded", False)
        hist = tmp_path / "h"
        hist.write_text("old\n", encoding="utf-8")
        inter._setup_readline_history(hist)
        assert inter._history_loaded is True
        monkeypatch.setattr(inter, "_history_loaded", False)

    def test_append_no_file(self, monkeypatch):
        monkeypatch.setattr(inter, "_HISTORY_FILE", None)
        assert inter._append_history("x") is None

    def test_command_persists(self, tmp_path, monkeypatch):
        loop, _, _ = _loop(tmp_path)
        _feed(monkeypatch, ["/help", "/exit"])
        loop.run()

    def test_hooks_scope(self, tmp_path):
        from lib.core.hooks import create_hook_runtime
        from lib.runtime import RuntimeContext

        loop, _, state = _loop(tmp_path)
        rt = RuntimeContext.from_app_state(state)
        rt.hooks = create_hook_runtime(tmp_path)
        state.runtime_context = rt
        assert loop.dispatch_command("/help") is True

    def test_append_broken(self, tmp_path, monkeypatch):
        from pathlib import Path as _Path

        monkeypatch.setattr(_Path, "mkdir",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
        inter._append_history("line", tmp_path / "h")
