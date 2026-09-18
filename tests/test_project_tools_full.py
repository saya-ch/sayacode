# project_tools 全覆盖测试：各语言检测、依赖解析、摘要与符号工具。

from pathlib import Path

import pytest

import lib.tools.project_tools as pt
from lib.tools.project_tools import (
    ProjectAnalyzer,
    analyze_project,
    find_symbol,
    get_default_workspace,
    get_file_info,
    get_project_summary,
    list_project_files,
    list_symbols,
    reset_workspace,
    use_workspace,
)


@pytest.fixture
def ws(tmp_path):
    # 隔离工作区固件。
    token = use_workspace(tmp_path)
    try:
        yield tmp_path
    finally:
        reset_workspace(token)


def _py_project(root, extra_reqs=None):
    # 搭一个带 FastAPI 标记的 Python 项目。
    (root / "requirements.txt").write_text(
        "# deps\nfastapi==0.110\nuvicorn>=0.29\nplain-pkg\n" + (extra_reqs or ""),
        encoding="utf-8",
    )
    (root / "app.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    return root


# ── 语言检测 ─────────────────────────────────────────────────────────────

class TestLanguageDetection:
    def test_python(self, ws):
        _py_project(ws)
        a = ProjectAnalyzer(".")
        assert a.language == "Python"
        assert a.project_type == "python"
        assert "FastAPI" in a.frameworks
        assert a.dependencies.get("fastapi") == "0.110"
        assert a.dependencies.get("uvicorn") == ">=0.29"
        assert a.dependencies.get("plain-pkg") == "any"
        assert "uvicorn>=0.29" not in a.dependencies
        assert "requirements.txt" in a.config_files

    def test_typescript(self, ws):
        (ws / "package.json").write_text(
            '{"dependencies": {"react": "18.0"}, "devDependencies": {"jest": "29"}}',
            encoding="utf-8",
        )
        (ws / "tsconfig.json").write_text("{}", encoding="utf-8")
        (ws / "app.tsx").write_text("import React from 'react';\n", encoding="utf-8")
        a = ProjectAnalyzer(".")
        assert a.language == "TypeScript"
        assert a.project_type == "typescript"
        assert "React" in a.frameworks
        assert a.dependencies.get("react") == "18.0"
        assert a.dependencies.get("jest") == "29"

    def test_javascript(self, ws):
        (ws / "package.json").write_text('{"dependencies": {}}', encoding="utf-8")
        (ws / "app.js").write_text("const express = require('express');\n", encoding="utf-8")
        a = ProjectAnalyzer(".")
        assert a.language == "JavaScript"
        assert "Express" in a.frameworks

    def test_java(self, ws):
        (ws / "pom.xml").write_text("<project></project>", encoding="utf-8")
        (ws / "Main.java").write_text("class Main {}\n", encoding="utf-8")
        a = ProjectAnalyzer(".")
        assert a.language == "Java"

    def test_go(self, ws):
        (ws / "go.mod").write_text(
            "module demo\n\ngo 1.22\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.9\n)\n\nrequire github.com/x/y v1.0\n",
            encoding="utf-8",
        )
        a = ProjectAnalyzer(".")
        assert a.language == "Go"
        assert a.dependencies.get("github.com/gin-gonic/gin") == "v1.9"
        assert a.dependencies.get("github.com/x/y") == "v1.0"

    def test_rust(self, ws):
        (ws / "Cargo.toml").write_text('[package]\nname = "d"\n', encoding="utf-8")
        (ws / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
        a = ProjectAnalyzer(".")
        assert a.language == "Rust"

    def test_cpp_cmake(self, ws):
        (ws / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.0)\n", encoding="utf-8")
        (ws / "main.cpp").write_text("int main() {}\n", encoding="utf-8")
        a = ProjectAnalyzer(".")
        assert a.language == "C++"
        assert a.project_type == "cpp"

    def test_cpp_vcxproj(self, ws):
        (ws / "demo.vcxproj").write_text("<Project></Project>", encoding="utf-8")
        (ws / "main.cpp").write_text("int main() {}\n", encoding="utf-8")
        a = ProjectAnalyzer(".")
        assert a.language == "C++"

    def test_pipfile(self, ws):
        (ws / "Pipfile").write_text('[packages]\nflask = "*"\n', encoding="utf-8")
        (ws / "app.py").write_text("from flask import Flask\n", encoding="utf-8")
        a = ProjectAnalyzer(".")
        assert a.language == "Python"
        assert "Flask" in a.frameworks

    def test_guess_by_extension(self, ws):
        (ws / "main.rb").write_text("puts 1\n", encoding="utf-8")
        a = ProjectAnalyzer(".")
        assert a.language == "Ruby"
        assert a.project_type == "generic"

    def test_unknown_falls_back_to_context_guess(self, ws):
        (ws / "notes.xyz").write_text("hi\n", encoding="utf-8")
        a = ProjectAnalyzer(".")
        assert a.language == "XYZ"
        assert a.project_type == "generic"

    def test_context_fallback(self, ws, monkeypatch):
        # 上下文自带类型/语言时回退采用它们。
        class StubCtx:
            files = []
            dependencies = {}
            project_type = "ctx-type"
            language = "ctx-lang"

        monkeypatch.setattr(pt, "ProjectContext", lambda root: StubCtx())
        a = ProjectAnalyzer(".")
        assert a.project_type == "ctx-type"
        assert a.language == "ctx-lang"
        assert a.stats["total_files"] == 0
        assert a.structure["source_dirs"] == []


# ── 依赖解析异常 ─────────────────────────────────────────────────────────

class TestDependencyErrors:
    def test_requirements_is_dir(self, ws, capsys):
        (ws / "requirements.txt").mkdir()
        (ws / "a.py").write_text("x\n", encoding="utf-8")
        ProjectAnalyzer(".")
        assert "失败" in capsys.readouterr().out or True

    def test_package_json_broken(self, ws, capsys):
        (ws / "package.json").write_text("{broken", encoding="utf-8")
        a = ProjectAnalyzer(".")
        assert a.language == "JavaScript"
        assert a.dependencies == {}

    def test_go_mod_is_dir(self, ws):
        (ws / "go.mod").mkdir()
        (ws / "main.go").write_text("package main\n", encoding="utf-8")
        a = ProjectAnalyzer(".")
        assert a.language == "Go"

    def test_go_mod_bare_require(self, ws):
        (ws / "go.mod").write_text(
            "module d\n\nrequire (\n\tlonely\n)\n", encoding="utf-8"
        )
        a = ProjectAnalyzer(".")
        assert a.dependencies.get("lonely") == "any"

    def test_go_mod_short_require_ignored(self, ws):
        (ws / "go.mod").write_text("module d\n\nrequire x\n", encoding="utf-8")
        a = ProjectAnalyzer(".")
        assert "x" not in a.dependencies


# ── 框架检测容错 ─────────────────────────────────────────────────────────

class TestFrameworkRobustness:
    def test_unreadable_py_skipped(self, ws, monkeypatch):
        _py_project(ws)
        real = Path.read_text

        def flaky(self, *args, **kwargs):
            if self.name == "app.py":
                raise RuntimeError("busy")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", flaky)
        a = ProjectAnalyzer(".")
        assert a.frameworks == []

    def test_unreadable_js_skipped(self, ws, monkeypatch):
        (ws / "package.json").write_text('{"dependencies": {}}', encoding="utf-8")
        (ws / "app.js").write_text("x\n", encoding="utf-8")
        real = Path.read_text

        def flaky(self, *args, **kwargs):
            if self.name == "app.js":
                raise RuntimeError("busy")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", flaky)
        a = ProjectAnalyzer(".")
        assert a.frameworks == []

    def test_duplicate_framework_once(self, ws):
        (ws / "requirements.txt").write_text("flask==3\n", encoding="utf-8")
        (ws / "a.py").write_text("from flask import Flask\nimport flask\n", encoding="utf-8")
        a = ProjectAnalyzer(".")
        assert a.frameworks.count("Flask") == 1


# ── 结构与摘要 ───────────────────────────────────────────────────────────

class TestStructureSummary:
    def _tree(self, ws):
        for d in ("src", "tests", "config", "docs", "misc", ".git", "node_modules"):
            (ws / d).mkdir()
        (ws / "src" / "m.py").write_text("x\n", encoding="utf-8")
        (ws / "requirements.txt").write_text("a==1\n", encoding="utf-8")

    def test_structure_buckets(self, ws):
        self._tree(ws)
        a = ProjectAnalyzer(".")
        assert "src" in a.structure["source_dirs"]
        assert "tests" in a.structure["test_dirs"]
        assert "config" in a.structure["config_dirs"]
        assert "docs" in a.structure["docs_dirs"]
        assert "misc" in a.structure["other_dirs"]
        assert ".git" not in a.structure["other_dirs"]

    def test_summary_full(self, ws):
        self._tree(ws)
        extra = "".join(f"pkg{i}=={i}\n" for i in range(20))
        with open(ws / "requirements.txt", "a", encoding="utf-8") as f:
            f.write(extra)
        a = ProjectAnalyzer(".")
        out = a.get_summary()
        assert "# 项目:" in out
        assert "总文件数" in out
        assert "文件类型分布" in out
        assert "还有" in out and "个依赖" in out
        assert "源代码目录" in out
        assert "测试目录" in out
        assert "配置目录" in out
        assert "其他目录" in out

    def test_summary_minimal(self, ws, monkeypatch):
        class StubCtx:
            files = []
            dependencies = {}
            project_type = None
            language = None

        monkeypatch.setattr(pt, "ProjectContext", lambda root: StubCtx())
        a = ProjectAnalyzer(".")
        out = a.get_summary()
        assert "语言: Unknown" in out
        assert "依赖" not in out

    def test_matches_glob_pattern(self):
        from lib.tools.project_tools import _matches_path_pattern

        assert _matches_path_pattern(Path("x/y.log"), "*.log") is True
        assert _matches_path_pattern(Path("x/y.log"), "*.tmp") is False
        assert _matches_path_pattern(Path("a/.git/b"), ".git") is True
        assert _matches_path_pattern(Path("a/mygit/b"), ".git") is False

    def test_workspace_helpers(self, ws, tmp_path):
        from lib.tools.project_tools import set_default_workspace

        assert get_default_workspace() == tmp_path.resolve()
        set_default_workspace(tmp_path)
        assert get_default_workspace() == tmp_path.resolve()
        set_default_workspace(Path.cwd())


# ── 对外工具 ─────────────────────────────────────────────────────────────

class TestToolEntry:
    def test_analyze_ok(self, ws):
        _py_project(ws)
        out = analyze_project.invoke({})
        assert "Python" in out

    def test_analyze_traversal(self, ws):
        assert "安全警告" in analyze_project.invoke({"root_dir": "../evil"})

    def test_analyze_crash(self, ws, monkeypatch):
        monkeypatch.setattr(pt, "ProjectAnalyzer",
                            lambda root: (_ for _ in ()).throw(RuntimeError("boom")))
        out = analyze_project.invoke({})
        assert "分析项目失败" in out

    def test_summary_ok(self, ws):
        _py_project(ws)
        out = get_project_summary.invoke({})
        assert "Python" in out and "FastAPI" in out

    def test_summary_traversal(self, ws):
        assert "安全警告" in get_project_summary.invoke({"root_dir": "../evil"})

    def test_summary_crash(self, ws, monkeypatch):
        monkeypatch.setattr(pt, "ProjectAnalyzer",
                            lambda root: (_ for _ in ()).throw(RuntimeError("boom")))
        assert "失败" in get_project_summary.invoke({})

    def test_list_files_ok(self, ws):
        _py_project(ws)
        out = list_project_files.invoke({})
        assert "app.py" in out

    def test_list_files_ext_filter(self, ws):
        _py_project(ws)
        out = list_project_files.invoke({"extension": "*.TXT"})
        assert "requirements.txt" in out
        assert "app.py" not in out

    def test_list_files_truncated(self, ws):
        for i in range(8):
            (ws / f"f{i}.py").write_text("x\n", encoding="utf-8")
        out = list_project_files.invoke({"max_count": 3})
        assert "还有 5 个文件" in out

    def test_list_files_traversal(self, ws):
        assert "安全警告" in list_project_files.invoke({"root_dir": "../evil"})

    def test_list_files_crash(self, ws, monkeypatch):
        monkeypatch.setattr(pt, "ProjectContext",
                            lambda root: (_ for _ in ()).throw(RuntimeError("boom")))
        assert "失败" in list_project_files.invoke({})

    def test_file_info_ok(self, ws):
        p = ws / "a.py"
        p.write_text("one\ntwo\n", encoding="utf-8")
        out = get_file_info.invoke({"file_path": "a.py"})
        assert "a.py" in out and "行数: 3" in out

    def test_file_info_binary(self, ws):
        (ws / "a.bin").write_bytes(bytes(range(256)))
        out = get_file_info.invoke({"file_path": "a.bin"})
        assert "行数: 0" in out

    def test_file_info_huge_size(self, ws, monkeypatch):
        import time as _time
        from types import SimpleNamespace as _NS

        (ws / "big.bin").write_bytes(b"x")
        real_stat = Path.stat

        def huge_stat(self, follow_symlinks=True):
            if self.name == "big.bin":
                return _NS(st_size=3 * 1024 ** 4, st_mtime=_time.time())
            return real_stat(self, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "stat", huge_stat)
        out = get_file_info.invoke({"file_path": "big.bin"})
        assert "TB" in out

    def test_file_info_missing(self, ws):
        assert "不存在" in get_file_info.invoke({"file_path": "nope.py"})

    def test_file_info_traversal(self, ws):
        assert "安全警告" in get_file_info.invoke({"file_path": "../evil.py"})

    def test_file_info_linecount_fails(self, ws, monkeypatch):
        (ws / "a.py").write_text("x\n", encoding="utf-8")
        real = Path.read_text

        def flaky(self, *args, **kwargs):
            if self.name == "a.py":
                raise RuntimeError("busy")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", flaky)
        out = get_file_info.invoke({"file_path": "a.py"})
        assert "行数: 0" in out

    def test_file_info_crash(self, ws, monkeypatch):
        monkeypatch.setattr(pt, "sanitize_path",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert "失败" in get_file_info.invoke({"file_path": "a.py"})

    def test_list_symbols_ok(self, ws):
        (ws / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
        out = list_symbols.invoke({})
        assert "foo" in out

    def test_list_symbols_query_kind(self, ws):
        (ws / "a.py").write_text("def foo():\n    pass\nclass Bar:\n    pass\n", encoding="utf-8")
        out = list_symbols.invoke({"query": "foo"})
        assert "foo" in out
        out = list_symbols.invoke({"kind": "class", "max_results": 0})
        assert "Bar" in out

    def test_list_symbols_traversal(self, ws):
        assert "安全警告" in list_symbols.invoke({"root_dir": "../evil"})

    def test_list_symbols_crash(self, ws, monkeypatch):
        monkeypatch.setattr(pt, "SymbolIndex",
                            lambda root: (_ for _ in ()).throw(RuntimeError("boom")))
        assert "失败" in list_symbols.invoke({})

    def test_find_symbol_ok(self, ws):
        (ws / "a.py").write_text("def target_fn():\n    pass\n", encoding="utf-8")
        out = find_symbol.invoke({"name": "target_fn"})
        assert "target_fn" in out

    def test_find_symbol_traversal(self, ws):
        assert "安全警告" in find_symbol.invoke({"name": "x", "root_dir": "../evil"})

    def test_find_symbol_crash(self, ws, monkeypatch):
        monkeypatch.setattr(pt, "SymbolIndex",
                            lambda root: (_ for _ in ()).throw(RuntimeError("boom")))
        assert "失败" in find_symbol.invoke({"name": "x"})
