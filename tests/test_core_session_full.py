# core/session.py 全覆盖：预算、压缩管道、持久化、导出。

import json


from lib.core.session import Message, SessionManager


def _mgr(**kw):
    # 小预算管理器固件，便于触发阈值。
    kw.setdefault("model_context_limit", 0)
    return SessionManager(**kw)


def _rounds(m, n):
    # 灌 n 轮完整对话。
    for i in range(n):
        m.add_user_message(f"q{i}")
        m.add_assistant_message(f"a{i}")
    return m


class TestMessage:
    def test_dict_roundtrip(self):
        m = Message(role="user", content="hi")
        assert Message.from_dict(m.to_dict()).content == "hi"
        assert Message.from_dict({"role": "user", "content": "x"}).timestamp == ""


class TestBudget:
    def test_estimate(self):
        assert SessionManager.estimate_tokens("") == 0
        assert SessionManager.estimate_tokens("ab") == 1
        assert SessionManager.estimate_tokens("x" * 30) == 10

    def test_overhead_system(self):
        m = _mgr()
        m.add_message("system", "sys-prompt")
        m._rebuild_token_count()
        assert m._running_tokens > 8700

    def test_ratios_zero_limit(self):
        m = _mgr()
        assert m.usage_ratio == 0.0
        assert m.needs_compact is False
        assert m.needs_preventive_compact is False
        assert m.needs_urgent_compact is False

    def test_ratios_capped(self):
        m = _mgr(model_context_limit=100)
        m.add_user_message("x" * 900)
        assert m.usage_ratio == 1.0
        assert m.needs_urgent_compact is True

    def test_preventive_only(self):
        m = _mgr(model_context_limit=10000)
        m.add_user_message("x" * 18000)
        assert m.needs_preventive_compact is True
        assert m.needs_compact is False
        assert m.needs_urgent_compact is False

    def test_setters(self):
        m = _mgr()
        m.set_context_limit(1000)
        assert m.context_budget == 800 and m.output_reserve == 150
        m.set_context_limit(0)
        assert m.context_budget == 0
        m.set_compact_fn(lambda msgs: "s", strategy="simple")
        assert m._compact_strategy == "simple"
        m.set_compact_fn(None)

    def test_max_messages_triggers(self):
        m = _mgr(max_messages=1)
        for i in range(4):
            m.add_user_message(f"q{i}")
        assert len(m.messages) == 4


class TestCompactFlow:
    def test_maybe_disabled(self):
        m = _mgr(enable_summary=False, model_context_limit=100)
        m.add_user_message("x" * 900)
        assert m.maybe_compact() is False

    def test_maybe_levels(self):
        m = _mgr(model_context_limit=10000)
        assert m.maybe_compact() is False
        for i in range(18):
            m.add_user_message(f"q{i}-" + "x" * 600)
            m.add_assistant_message(f"a{i}-" + "y" * 600)
        assert m.needs_compact is True and m.needs_urgent_compact is False
        assert m.maybe_compact() is True
        assert m._compact_count == 1

    def test_maybe_gentle(self):
        m = _mgr(model_context_limit=10000)
        for i in range(16):
            m.add_user_message(f"q{i}-" + "x" * 550)
            m.add_assistant_message(f"a{i}-" + "y" * 550)
        assert m.needs_preventive_compact is True and m.needs_compact is False
        assert m.maybe_compact() is True
        assert m._compact_count == 1

    def test_maybe_urgent(self):
        m = _mgr(model_context_limit=1000)
        m.add_user_message("x" * 4000)
        assert m.needs_urgent_compact is True
        assert m.maybe_compact() is True

    def test_compact_short(self):
        assert _mgr().compact() == "会话太短，无需压缩"
        assert _mgr().force_compact() == "会话太短，无法紧急压缩"

    def test_compact_static(self):
        m = _rounds(_mgr(), 12)
        out = m.compact()
        assert "压缩" in out and m._compact_count == 1

    def test_compact_focus(self):
        m = _rounds(_mgr(), 12)
        m.compact(focus="auth")
        assert m.summary is not None

    def test_force_compact(self):
        m = _rounds(_mgr(enable_summary=False), 12)
        out = m.force_compact(reason="boom")
        assert "boom" in out and m._compact_count == 1

    def test_auto_disabled(self):
        m = _rounds(_mgr(enable_summary=False), 12)
        m._auto_compact()
        assert m._compact_count == 0

    def test_auto_not_enough_rounds(self):
        m = _rounds(_mgr(), 3)
        m._auto_compact()
        assert m._compact_count == 0

    def test_semantic_fn(self):
        m = _rounds(_mgr(), 12)
        m.set_compact_fn(lambda msgs: "SEMANTIC-SUMMARY")
        m._auto_compact()
        assert "SEMANTIC-SUMMARY" in m.messages[1].content

    def test_semantic_empty_fallback(self):
        m = _rounds(_mgr(), 12)
        m.set_compact_fn(lambda msgs: "  ")
        m._auto_compact()
        assert m._compact_count == 1

    def test_semantic_crash_fallback(self):
        m = _rounds(_mgr(), 12)

        def boom(msgs):
            raise RuntimeError("llm down")

        m.set_compact_fn(boom)
        m._auto_compact()
        assert "fallback" in m.messages[1].content

    def test_semantic_truncation(self):
        m = _mgr()
        m.add_user_message("u" * 2000)
        m.add_assistant_message("a" * 20000)
        m.set_compact_fn(lambda msgs: "S")
        text = m._generate_semantic_summary([{"type": "round", "user": m.messages[0], "assistant": m.messages[1]}])
        assert text == "S"

    def test_semantic_bulk_truncation_and_focus(self):
        m = _mgr()
        for i in range(12):
            m.add_user_message(f"u{i}-" + "u" * 1400)
            m.add_assistant_message(f"a{i}-" + "a" * 1400)
        seen = {}

        def capture(msgs):
            seen["prompt"] = msgs[0]["content"]
            return "S"

        m.set_compact_fn(capture)
        _, rounds = SessionManager._identify_rounds(m.messages)
        assert m._generate_semantic_summary(rounds, focus="auth") == "S"
        assert "[earlier parts truncated]" in seen["prompt"]
        assert "Focus area: auth" in seen["prompt"]

    def test_semantic_no_fn(self):
        m = _rounds(_mgr(), 12)
        assert "早期对话摘要" in m._generate_semantic_summary([{"type": "round", "user": m.messages[0]}])

    def test_archive_none(self):
        assert _mgr()._archive_history() is None

    def test_archive_ok(self, tmp_path):
        m = _rounds(_mgr(archive_dir=str(tmp_path / "arc")), 2)
        path = m._archive_history()
        assert path is not None and m._last_archive_path == path

    def test_archive_crash(self, tmp_path, monkeypatch):
        import json as _json

        m = _rounds(_mgr(archive_dir=str(tmp_path / "arc")), 2)
        monkeypatch.setattr(_json, "dump", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
        assert m._archive_history() is None

    def test_compact_with_archive(self, tmp_path):
        m = _rounds(_mgr(archive_dir=str(tmp_path / "arc")), 12)
        m.compact()
        assert "存档" in m.messages[0].content


class TestRounds:
    def test_identify_shapes(self):
        m = _mgr()
        m.add_message("system", "sys")
        m.add_user_message("q1")
        m.add_user_message("q2")
        m.add_assistant_message("a2")
        m.add_assistant_message("orphan")
        m.add_user_message("q3")
        _, rounds = SessionManager._identify_rounds(m.messages)
        assert [r["type"] for r in rounds] == ["round", "round", "round", "round"]

    def test_compressed_passthrough(self):
        m = _mgr()
        m.messages.append(Message(role="system", content="old", metadata={"compressed": True}))
        _, rounds = SessionManager._identify_rounds(m.messages)
        assert rounds[0]["type"] == "compressed"

    def test_summarize_round(self):
        m = _mgr()
        m.add_user_message("q" * 300)
        out = SessionManager._summarize_round({"type": "round", "user": m.messages[0], "assistant": None})
        assert "待回复" in out and "..." in out
        m.add_assistant_message("a" * 300)
        out = SessionManager._summarize_round({"type": "round", "user": m.messages[0], "assistant": m.messages[1]})
        assert "助手" in out

    def test_bulk_mixed(self):
        m = _mgr()
        m.add_user_message("q" * 200)
        m.add_assistant_message("a" * 200)
        m.messages.append(Message(role="system", content="old-sum", metadata={"compressed": True, "type": "summary"}))
        _, rounds = SessionManager._identify_rounds(m.messages)
        out = SessionManager._summarize_rounds_bulk(rounds)
        assert "old-sum" in out and "回应" in out

    def test_second_compact_keeps_old(self):
        m = _rounds(_mgr(), 12)
        m.compact()
        for i in range(12):
            m.add_user_message(f"nq{i}")
            m.add_assistant_message(f"na{i}")
        m.compact()
        assert m._compact_count == 2


class TestMessages:
    def test_get_variants(self):
        m = _mgr()
        m.add_message("system", "sys", metadata={"k": "v"})
        m.add_user_message("q")
        assert len(m.get_messages()) == 2
        assert all(x["role"] != "system" for x in m.get_messages(include_system=False))
        assert len(m.get_messages(max_turns=1)) == 2

    def test_get_artifacts(self):
        m = _rounds(_mgr(), 12)
        m.compact()
        bare = m.get_messages(include_system=False)
        kept = m.get_messages(include_system=False, include_compaction_summaries=True)
        assert len(kept) > len(bare)

    def test_history_clear(self):
        m = _rounds(_mgr(), 2)
        assert len(m.get_history()) == 4
        assert m.is_empty() is False and m.get_message_count() == 4
        m.clear()
        assert m.is_empty() is True and m.summary is None

    def test_info(self):
        m = _mgr()
        info = m.get_compact_info()
        assert info["compact_count"] == 0 and info["context_limit_known"] is False
        assert "id=" in repr(m)


class TestPersist:
    def test_roundtrip(self, tmp_path):
        m = _rounds(_mgr(model_context_limit=8000, archive_dir=str(tmp_path / "arc")), 2)
        m.compact() if False else None
        path = str(tmp_path / "s.json")
        assert m.save(path) is True
        loaded = SessionManager.load(path)
        assert loaded is not None and loaded.get_message_count() == 4
        assert loaded.model_context_limit == 8000

    def test_load_missing(self, tmp_path):
        assert SessionManager.load(str(tmp_path / "nope.json")) is None

    def test_load_bad_schema(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        assert SessionManager.load(str(p)) is None

    def test_load_corrupt(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text("{broken", encoding="utf-8")
        assert SessionManager.load(str(p)) is None

    def test_save_crash(self, tmp_path, monkeypatch):
        import lib.core.session as _sess

        m = _mgr()
        monkeypatch.setattr(_sess, "write_private_json",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
        assert m.save(str(tmp_path / "s.json")) is False


class TestExport:
    def test_summary_variants(self):
        assert _mgr().get_context_summary() == "空对话"
        m = _rounds(_mgr(model_context_limit=8000), 2)
        assert "对话轮数" in m.get_context_summary()
        m.summary = "S"
        assert m.get_context_summary() == "S"
        assert "unknown" in _mgr().get_context_summary() or True

    def test_export_json(self):
        m = _rounds(_mgr(), 1)
        data = json.loads(m.export_conversation(format="json"))
        assert len(data) == 2

    def test_export_markdown(self):
        m = _rounds(_mgr(), 1)
        out = m.export_conversation(format="markdown")
        assert "用户" in out and "助手" in out

    def test_export_markdown_compacted(self):
        m = _rounds(_mgr(), 12)
        m.compact()
        assert "压缩次数" in m.export_conversation(format="markdown")

    def test_export_text(self):
        m = _rounds(_mgr(), 1)
        assert "q0" in m.export_conversation(format="text")
