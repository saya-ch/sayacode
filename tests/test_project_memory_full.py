# project_memory 全覆盖上半：发现、加载、截断。


import lib.core.project_memory as pm
from lib.core.project_memory import (
    discover_project_memory_paths,
    load_memory_files,
    user_memory_path,
)


class TestFiles:
    def test_user_path_create(self):
        p = user_memory_path(create_parent=True)
        assert p.name == "memory.md" and p.parent.exists()

    def test_user_file_loaded(self, tmp_path):
        p = user_memory_path(create_parent=True)
        p.write_text("user pref", encoding="utf-8")
        files = load_memory_files(tmp_path)
        assert any(f.label == "User memory" for f in files)

    def test_project_and_compat(self, tmp_path):
        (tmp_path / "SAYACODE.md").write_text("proj", encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("compat", encoding="utf-8")
        labels = [f.label for f in load_memory_files(tmp_path, include_user=False)]
        assert "Project memory" in labels and "Compatibility memory" in labels

    def test_file_no_truncation(self, tmp_path):
        (tmp_path / "SAYACODE.md").write_text("x" * 13000, encoding="utf-8")
        files = load_memory_files(tmp_path, include_user=False)
        assert files[0].truncated is False and len(files[0].content) == 13000

    def test_total_limit_passthrough(self, tmp_path, monkeypatch):
        # 总量限流已废弃（剪枝走官方中间件）：常量不再生效，全量返回。
        (tmp_path / "SAYACODE.md").write_text("a" * 500, encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("b" * 500, encoding="utf-8")
        monkeypatch.setattr(pm, "MAX_MEMORY_TOTAL_CHARS", 600)
        files = load_memory_files(tmp_path, include_user=False)
        assert len(files) == 2 and all(f.truncated is False for f in files)
        monkeypatch.setattr(pm, "MAX_MEMORY_TOTAL_CHARS", 0)
        assert len(load_memory_files(tmp_path, include_user=False)) == 2

    def test_discover_upwards(self, tmp_path):
        (tmp_path / "SAYACODE.md").write_text("top", encoding="utf-8")
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        found = discover_project_memory_paths(sub)
        assert tmp_path / "SAYACODE.md" in found

    def test_user_is_dir_skipped(self, tmp_path, monkeypatch):
        import lib.core.project_memory as _pm

        monkeypatch.setattr(_pm, "user_memory_path", lambda create_parent=False: tmp_path)
        assert all(f.label != "User memory" for f in load_memory_files(tmp_path))

    def test_dir_not_file_skipped(self, tmp_path):
        (tmp_path / "SAYACODE.md").mkdir()
        assert load_memory_files(tmp_path, include_user=False) == []


class TestImports:
    def test_cycle(self, tmp_path):
        (tmp_path / "SAYACODE.md").write_text("see @b.md", encoding="utf-8")
        (tmp_path / "b.md").write_text("see @SAYACODE.md", encoding="utf-8")
        files = load_memory_files(tmp_path, include_user=False)
        assert "imported" in files[0].content

    def test_depth(self, tmp_path):
        from lib.core.project_memory import _read_with_imports

        assert _read_with_imports(tmp_path / "x", depth=6, seen=set()) == ""

    def test_unreadable(self, tmp_path):
        from lib.core.project_memory import _read_with_imports

        d = tmp_path / "sub"
        d.mkdir()
        assert _read_with_imports(d, depth=0, seen=set()) == ""

    def test_code_block_ignored(self, tmp_path):
        (tmp_path / "SAYACODE.md").write_text("```\n@notimport.md\n```\nreal @b.md", encoding="utf-8")
        (tmp_path / "b.md").write_text("B", encoding="utf-8")
        files = load_memory_files(tmp_path, include_user=False)
        assert "@notimport.md" in files[0].content and "B" in files[0].content

    def test_missing_import_kept(self, tmp_path):
        (tmp_path / "SAYACODE.md").write_text("see @ghost.md", encoding="utf-8")
        files = load_memory_files(tmp_path, include_user=False)
        assert "@ghost.md" in files[0].content

    def test_blocked_imports(self, tmp_path):
        (tmp_path / ".env").write_text("K=V", encoding="utf-8")
        (tmp_path / "SAYACODE.md").write_text("see @.env", encoding="utf-8")
        files = load_memory_files(tmp_path, include_user=False)
        assert "blocked" in files[0].content
        assert "K=V" not in files[0].content

    def test_outside_root_blocked(self, tmp_path):
        (tmp_path.parent / "evil.md").write_text("SECRET", encoding="utf-8")
        (tmp_path / "SAYACODE.md").write_text("see @../evil.md", encoding="utf-8")
        files = load_memory_files(tmp_path, include_user=False)
        assert "blocked" in files[0].content
        assert "SECRET" not in files[0].content

    def test_extract_paths(self):
        from lib.core.project_memory import _extract_import_path

        assert _extract_import_path("see `@x` now") is None
        assert _extract_import_path("see @b.md,") == "b.md"
        assert _extract_import_path("nothing") is None
        assert _extract_import_path("see @sub/dir.md;") == "sub/dir.md"

    def test_resolve_fail(self, tmp_path, monkeypatch):
        from pathlib import Path as _Path
        from lib.core.project_memory import _resolve_import_path

        real_resolve = _Path.resolve

        def flaky(self, *args, **kwargs):
            if self.name == "bad.md":
                raise OSError("busy")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(_Path, "resolve", flaky)
        assert _resolve_import_path("bad.md", tmp_path) is None
        assert _resolve_import_path("ok.md", tmp_path) is not None

    def test_allowed_variants(self, tmp_path, monkeypatch):
        from pathlib import Path as _Path
        from lib.core.project_memory import _memory_import_allowed

        assert _memory_import_allowed(tmp_path / "a.md", None) is True
        assert _memory_import_allowed(tmp_path / "a.md", tmp_path) is True
        assert _memory_import_allowed(tmp_path / ".env", tmp_path) is False
        monkeypatch.setattr(_Path, "resolve", lambda self, *a, **k: (_ for _ in ()).throw(OSError("x")))
        assert _memory_import_allowed(tmp_path / "a.md", tmp_path) is False


class TestRenderAppend:
    def test_prompt_empty(self, tmp_path):
        from lib.core.project_memory import render_memory_for_prompt

        assert render_memory_for_prompt(tmp_path) == ""

    def test_prompt_files(self, tmp_path):
        from lib.core.project_memory import render_memory_for_prompt

        (tmp_path / "SAYACODE.md").write_text("  ", encoding="utf-8")
        assert "(empty)" in render_memory_for_prompt(tmp_path)
        (tmp_path / "SAYACODE.md").write_text("x" * 13000, encoding="utf-8")
        assert "x" * 13000 in render_memory_for_prompt(tmp_path)

    def test_status(self, tmp_path):
        from lib.core.project_memory import render_memory_status

        out = render_memory_status(tmp_path)
        assert "missing" in out and "No project memory" in out
        (tmp_path / "SAYACODE.md").write_text("x", encoding="utf-8")
        out = render_memory_status(tmp_path)
        assert "present" in out and "Loaded:" in out

    def test_init_append(self, tmp_path):
        from lib.core.project_memory import append_project_memory, append_user_memory, initialize_project_memory

        p = initialize_project_memory(tmp_path)
        assert p.exists()
        assert initialize_project_memory(tmp_path) == p
        append_project_memory(tmp_path, "rule one")
        assert "rule one" in p.read_text(encoding="utf-8")
        append_user_memory("user rule")
        assert "user rule" in user_memory_path().read_text(encoding="utf-8")
        append_user_memory("   ")

    def test_with_entry(self):
        from lib.core.project_memory import _with_entry

        assert _with_entry("a", "") == "a\n"
        assert _with_entry("", "x") == "- x\n"
        assert _with_entry("a", "x") == "a\n\n- x\n"
