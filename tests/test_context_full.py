# core/context.py 全覆盖：扫描、排除、统计、持久化。

from pathlib import Path


from lib.core.context import ChangeRecord, ProjectContext, _matches_excluded_pattern


class TestScan:
    def test_missing_root(self, tmp_path):
        ctx = ProjectContext(str(tmp_path / "nope"))
        assert ctx.files == []
        assert repr(ctx) != ""

    def test_resolve_fails(self, tmp_path, monkeypatch):
        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        real_resolve = Path.resolve
        calls = {"n": 0}

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("busy")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", flaky)
        ctx = ProjectContext(str(tmp_path))
        assert ctx.files != []

    def test_walk_error(self, tmp_path, monkeypatch):
        import os as _os

        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        def fake_walk(*args, **kwargs):
            cb = kwargs.get("onerror")
            if cb:
                cb(OSError("busy"))
            return iter([])

        monkeypatch.setattr(_os, "walk", fake_walk)
        ctx = ProjectContext(str(tmp_path))
        assert ctx.files == []

    def test_relative_fails(self, tmp_path, monkeypatch):
        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
        real_rel = Path.relative_to
        calls = {"n": 0}

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("busy")
            return real_rel(self, *args, **kwargs)

        monkeypatch.setattr(Path, "relative_to", flaky)
        ctx = ProjectContext(str(tmp_path))
        assert ctx.files != []

    def test_shallow_home(self, tmp_path, monkeypatch):
        deep = tmp_path
        for i in range(5):
            deep = deep / f"d{i}"
            deep.mkdir()
        (deep / "a.py").write_text("x\n", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        ctx = ProjectContext(str(tmp_path))
        assert all(len(Path(f.path).parts) <= 4 for f in ctx.files)

    def test_exclude_outside(self, tmp_path):
        ctx = ProjectContext(str(tmp_path))
        assert ctx._should_exclude(Path("/outside/x.py")) is False
        assert ctx._should_exclude(tmp_path / ".git" / "x") is True

    def test_linecount_fails(self, tmp_path, monkeypatch):
        import builtins as _bi

        (tmp_path / "locked.py").write_text("x\n", encoding="utf-8")
        real_open = _bi.open

        def flaky_open(path, *args, **kwargs):
            if str(path).endswith("locked.py") and "r" in (args[0] if args else kwargs.get("mode", "r")):
                raise OSError("busy")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(_bi, "open", flaky_open)
        ctx = ProjectContext(str(tmp_path))
        assert ctx.get_file_by_path("locked.py").line_count == 0

    def test_stat_fails(self, tmp_path, monkeypatch):
        (tmp_path / "gone.py").write_text("x\n", encoding="utf-8")
        real_stat = Path.stat

        def flaky(self, *args, **kwargs):
            if self.name == "gone.py":
                raise OSError("busy")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", flaky)
        ctx = ProjectContext(str(tmp_path))
        assert ctx.get_file_by_path("gone.py") is None

    def test_excluded_variants(self, tmp_path):
        (tmp_path / "a.pyc").write_text("x", encoding="utf-8")
        (tmp_path / ".env").write_text("x", encoding="utf-8")
        (tmp_path / "id_rsa").write_text("x", encoding="utf-8")
        ctx = ProjectContext(str(tmp_path))
        assert ctx.get_file_by_path("a.pyc") is None
        assert _matches_excluded_pattern(Path("sub/x.key"), "sub/*.key") is True
        assert _matches_excluded_pattern(Path("sub/x.key"), "other/*.key") is False


class TestContent:
    def _proj(self, root, deps=12):
        (root / "requirements.txt").write_text("\n".join(f"pkg{i}=={i}" for i in range(deps)), encoding="utf-8")
        for i in range(7):
            (root / f"m{i}.py").write_text("x\n", encoding="utf-8")
        return root

    def test_full_context(self, tmp_path):
        self._proj(tmp_path)
        ctx = ProjectContext(str(tmp_path))
        ctx.track_change("modified", "m0.py", description="fix", details={"k": "v"})
        ctx.track_change("created", "m1.py")
        out = ctx.get_context_for_llm()
        assert "还有 2 个依赖" in out
        assert "还有 2 个 py 文件" in out
        assert "fix" in out
        assert ChangeRecord(timestamp="t", action="a", file_path="f").to_dict()["file"] == "f"

    def test_summary_stats(self, tmp_path):
        self._proj(tmp_path, deps=2)
        ctx = ProjectContext(str(tmp_path))
        assert "2 个依赖" in ctx.get_summary()
        stats = ctx.get_statistics()
        assert stats["total_files"] >= 8 and stats["by_type"]["py"]["count"] == 7
        assert ctx.get_file_by_path("nope.py") is None
        assert ctx.get_file_by_path("m0.py").name == "m0.py"

    def test_save_fail(self, tmp_path):
        ctx = ProjectContext(str(tmp_path))
        assert ctx.save_context(str(tmp_path)) is False
        assert ctx.save_context(str(tmp_path / "ctx.json")) is True
