# session_store 全覆盖：索引、解析、加载、持久化、挂载。

from pathlib import Path
from types import SimpleNamespace


from lib.core.session_messages import SessionManager
from lib.runtime.session_store import (
    attach_session_to_runtime,
    create_session,
    derive_session_title,
    list_workspace_sessions,
    load_runtime_managers,
    load_session_memory_pair,
    load_workspace_session_index,
    new_workspace_session_index,
    persist_local_state,
    resolve_workspace_session_id,
    save_runtime_state,
    session_index_entry,
    sync_session_model_runtime,
    upsert_workspace_session_index,
    workspace_session_paths,
    workspace_state_paths,
)


def _pair(ws, **kw):
    # 存 session 并返回其派生记忆视图。
    from lib.core.session_messages import SessionDerivedMemoryView

    session = create_session(ws, **kw)
    return session, SessionDerivedMemoryView(session)


class TestIndex:
    def test_new_index(self, tmp_path):
        index = new_workspace_session_index(tmp_path)
        assert index["sessions"] == [] and index["active_session_id"] is None

    def test_missing_file(self, tmp_path):
        assert load_workspace_session_index(tmp_path)["sessions"] == []

    def test_corrupt_file(self, tmp_path):
        paths = workspace_state_paths(tmp_path)
        paths["dir"].mkdir(parents=True, exist_ok=True)
        paths["index"].write_text("{broken", encoding="utf-8")
        assert load_workspace_session_index(tmp_path)["sessions"] == []

    def test_bad_shape(self, tmp_path):

        paths = workspace_state_paths(tmp_path)
        paths["dir"].mkdir(parents=True, exist_ok=True)
        paths["index"].write_text('{"sessions": "oops", "workspace": ""}', encoding="utf-8")
        index = load_workspace_session_index(tmp_path)
        assert index["sessions"] == [] and index["workspace"] != ""

    def test_non_dict(self, tmp_path):
        paths = workspace_state_paths(tmp_path)
        paths["dir"].mkdir(parents=True, exist_ok=True)
        paths["index"].write_text("[1, 2]", encoding="utf-8")
        assert load_workspace_session_index(tmp_path)["sessions"] == []

    def test_upsert_roundtrip(self, tmp_path):
        session, memory = _pair(tmp_path)
        upsert_workspace_session_index(tmp_path, session, memory, title="t")
        entries = list_workspace_sessions(tmp_path)
        assert len(entries) == 1 and entries[0]["title"] == "t"
        upsert_workspace_session_index(tmp_path, session, memory, title="t2")
        assert len(list_workspace_sessions(tmp_path)) == 1

    def test_list_empty(self, tmp_path):
        assert list_workspace_sessions(tmp_path) == []

    def test_title_from_messages(self, tmp_path):
        session, _ = _pair(tmp_path)
        session.add_user_message("  hello   world  ")
        assert derive_session_title(session) == "hello world"
        assert derive_session_title(session, fallback="  fb  ") == "fb"
        fresh, _ = _pair(tmp_path)
        assert derive_session_title(fresh) == f"Session {fresh.session_id}"

    def test_entry_relative_fallback(self, tmp_path, monkeypatch):
        import lib.runtime.session_store as _ss

        session, memory = _pair(tmp_path)
        monkeypatch.setattr(_ss, "workspace_session_paths",
                            lambda ws, sid: {"session": Path("/outside/s.json"),
                                           "memory": Path("/outside/m.json"),
                                           "context": Path("/outside/c.json")})
        entry = session_index_entry(tmp_path, session, memory)
        assert entry["session_path"] == str(Path("/outside/s.json"))


class TestResolve:
    def test_skips_non_dict(self, tmp_path):
        from lib.runtime.session_store import write_workspace_session_index

        session, memory = _pair(tmp_path)
        upsert_workspace_session_index(tmp_path, session, memory)
        index = load_workspace_session_index(tmp_path)
        index["sessions"].append("oops")
        index["sessions"].append({"no_id": 1})
        write_workspace_session_index(tmp_path, index)
        assert resolve_workspace_session_id(tmp_path, session.session_id) == session.session_id

    def test_prefix_and_files(self, tmp_path):
        session, memory = _pair(tmp_path)
        save_runtime_state(SimpleNamespace(workspace=tmp_path, session=session, memory=memory, context=None))
        assert resolve_workspace_session_id(tmp_path, session.session_id[:8]) == session.session_id
        assert resolve_workspace_session_id(tmp_path, "ghost") is None

    def test_active(self, tmp_path):
        session, memory = _pair(tmp_path)
        upsert_workspace_session_index(tmp_path, session, memory)
        assert resolve_workspace_session_id(tmp_path) == session.session_id

    def test_files_without_index(self, tmp_path):
        from lib.runtime.session_store import workspace_state_paths

        session, memory = _pair(tmp_path)
        save_runtime_state(SimpleNamespace(workspace=tmp_path, session=session, memory=memory, context=None))
        (workspace_state_paths(tmp_path)["index"]).unlink()
        assert resolve_workspace_session_id(tmp_path, session.session_id) == session.session_id

    def test_last_or_none(self, tmp_path):
        assert resolve_workspace_session_id(tmp_path) is None

    def test_bad_paths(self, tmp_path, monkeypatch):
        import lib.runtime.session_store as _ss

        session, memory = _pair(tmp_path)
        upsert_workspace_session_index(tmp_path, session, memory)
        monkeypatch.setattr(_ss, "workspace_session_paths",
                            lambda ws, sid: (_ for _ in ()).throw(ValueError("bad id")))
        assert resolve_workspace_session_id(tmp_path, "a/b") is None


class TestLoad:
    def test_fresh_pair(self, tmp_path):
        session, memory, restored = load_session_memory_pair(tmp_path, "brand-new-id")
        assert restored is False and session.session_id == "brand-new-id"

    def test_roundtrip(self, tmp_path):
        session, memory = _pair(tmp_path)
        session.add_user_message("q")
        session.add_assistant_message("a")
        save_runtime_state(SimpleNamespace(workspace=tmp_path, session=session, memory=memory, context=None))
        session2, memory2, restored = load_session_memory_pair(tmp_path, session.session_id)
        assert restored is True and len(memory2) == 1

    def test_legacy_memory_unreadable(self, tmp_path):
        # 会话文件缺失 + 旧记忆损坏：不崩，返回新会话。
        session, _ = _pair(tmp_path)
        paths = workspace_session_paths(tmp_path, session.session_id)
        paths["memory"].parent.mkdir(parents=True, exist_ok=True)
        paths["memory"].mkdir()
        _, _, restored = load_session_memory_pair(tmp_path, session.session_id)
        assert restored is False

    def test_legacy_memory_imported_when_session_missing(self, tmp_path):
        import json as _json

        session, _ = _pair(tmp_path)
        paths = workspace_session_paths(tmp_path, session.session_id)
        paths["memory"].parent.mkdir(parents=True, exist_ok=True)
        paths["memory"].write_text(_json.dumps({
            "session_id": session.session_id,
            "interactions": [{"timestamp": "t", "user_input": "q", "ai_response": "a",
                               "tools_used": [], "modified_files": []}],
        }), encoding="utf-8")
        session2, memory2, restored = load_session_memory_pair(tmp_path, session.session_id)
        assert restored is True and len(memory2) == 1

    def test_managers_create_new(self, tmp_path):
        session, _, restored = load_runtime_managers(tmp_path, create_new=True)
        assert restored is False and session is not None

    def test_managers_fresh(self, tmp_path):
        _, _, restored = load_runtime_managers(tmp_path)
        assert restored is False

    def test_managers_active(self, tmp_path):
        session, memory = _pair(tmp_path)
        save_runtime_state(SimpleNamespace(workspace=tmp_path, session=session, memory=memory, context=None))
        upsert_workspace_session_index(tmp_path, session, memory)
        session2, _, restored = load_runtime_managers(tmp_path)
        assert restored is True and session2.session_id == session.session_id


class TestPersistAttach:
    def test_persist_with_config(self, tmp_path):
        from lib.runtime.state import UserConfig

        session, memory = _pair(tmp_path)
        state = SimpleNamespace(workspace=tmp_path, session=session, memory=memory, context=None,
                                active_profile=None, stream_output=True, confirm_dangerous=True,
                                prompt_style="standard", agent_mode="build")
        cfg = UserConfig()
        persist_local_state(state, cfg)
        assert cfg.workspace == str(tmp_path)

    def test_persist_with_context(self, tmp_path):
        from lib.core.context import ProjectContext

        session, memory = _pair(tmp_path)
        ctx = ProjectContext(str(tmp_path))
        state = SimpleNamespace(workspace=tmp_path, session=session, memory=memory, context=ctx,
                                active_profile=None, stream_output=True, confirm_dangerous=True,
                                prompt_style="standard", agent_mode="build")
        persist_local_state(state, None)

    def test_sync_model(self):
        session = SessionManager()
        sync_session_model_runtime(session, object())
        sync_session_model_runtime(session, SimpleNamespace(context_window=8000, chat=lambda m: m))

    def test_attach_full(self, tmp_path):
        from lib.runtime.state import create_app_state

        session, memory = _pair(tmp_path)
        state = create_app_state(tmp_path)
        agent = SimpleNamespace(session=None, model=None, memory=None,
                                conversation_manager=SimpleNamespace(session=None, memory=None))
        attach_session_to_runtime(agent, state, session, memory, restored=True)
        assert state.session is session and agent.memory is memory
        assert state.restored_session is True

    def test_attach_bare(self, tmp_path):
        from lib.runtime.state import create_app_state

        session, memory = _pair(tmp_path)
        state = create_app_state(tmp_path)
        attach_session_to_runtime(object(), state, session, memory, restored=False)
        assert state.session is session

    def test_attach_runtime_context(self, tmp_path):
        from lib.runtime.state import create_app_state

        session, memory = _pair(tmp_path)
        state = create_app_state(tmp_path)
        seen = {}
        state.runtime_context = SimpleNamespace(
            sync_from_app_state=lambda s: seen.update(synced=True),
            attach_agent=lambda a: seen.update(attached=True),
        )
        attach_session_to_runtime(object(), state, session, memory, restored=False)
        assert seen == {"synced": True, "attached": True}

    def test_create_session_shapes(self, tmp_path):
        assert create_session(tmp_path).archive_dir is not None
        assert create_session(None) is not None
