# doctor 全覆盖：详情本地化逐分支 + 各项检查正反路径。

import json
import sys


import lib.core.doctor as doc
from lib.core.doctor import DiagnosticCheck


def _check(name, status, detail):
    # 详情本地化固件。
    return DiagnosticCheck(name, status, detail)


class TestDetail:
    def test_names(self):
        assert doc._doctor_check_name("Git Repository") != ""
        assert doc._doctor_check_name("Weird Name!") == "Weird Name!"

    def test_python(self):
        assert "3.13" in doc._doctor_check_detail(_check("Python", "ok", "3.13.1 satisfies >= 3.11"))
        assert "3.10" in doc._doctor_check_detail(_check("Python", "fail", "3.10.0 is too old; Python >= 3.11 is required"))
        assert doc._doctor_check_detail(_check("Python", "ok", "other")) == "other"

    def test_package(self):
        assert doc._doctor_check_detail(_check("Package", "warn", "not installed as a package; running from source checkout")) != ""
        assert "1.0" in doc._doctor_check_detail(_check("Package", "ok", "sayacode 1.0 (source checkout; installed distribution 2.0)"))
        assert doc._doctor_check_detail(_check("Package", "ok", "sayacode 1.0")) == "sayacode 1.0"

    def test_workspace(self):
        assert "/x" in doc._doctor_check_detail(_check("Workspace", "ok", "/x is writable"))
        assert "/x" in doc._doctor_check_detail(_check("Workspace", "fail", "/x does not exist"))
        assert "/x" in doc._doctor_check_detail(_check("Workspace", "fail", "/x is not a directory"))
        assert "disk" in doc._doctor_check_detail(_check("Workspace", "fail", "/x is not writable: disk"))
        assert doc._doctor_check_detail(_check("Workspace", "ok", "other")) == "other"

    def test_git(self):
        assert doc._doctor_check_detail(_check("Git", "warn", "git executable was not found in PATH")) != ""
        assert doc._doctor_check_detail(_check("Git", "warn", "git returned a non-zero exit code")) != ""
        assert "boom" in doc._doctor_check_detail(_check("Git", "warn", "git exists but could not run: boom"))
        assert doc._doctor_check_detail(_check("Git", "ok", "git version 1")) == "git version 1"

    def test_git_repo(self):
        assert doc._doctor_check_detail(_check("Git Repository", "warn", "skipped")) != ""
        assert "/x" in doc._doctor_check_detail(_check("Git Repository", "ok", "/x is inside a Git work tree"))
        assert doc._doctor_check_detail(_check("Git Repository", "warn", "workspace is not a Git repository yet")) != ""
        assert doc._doctor_check_detail(_check("Git Repository", "ok", "other")) == "other"

    def test_config_dir(self):
        assert "/x" in doc._doctor_check_detail(_check("Config Directory", "ok", "/x is available"))
        assert "disk" in doc._doctor_check_detail(_check("Config Directory", "fail", "/x is not writable: disk"))
        assert doc._doctor_check_detail(_check("Config Directory", "ok", "other")) == "other"

    def test_config_schema(self):
        assert doc._doctor_check_detail(_check("Config Schema", "ok", "current config schema")) != ""
        assert doc._doctor_check_detail(_check("Config Schema", "warn", "legacy config detected; x")) != ""
        assert doc._doctor_check_detail(_check("Config Schema", "ok", "other")) == "other"

    def test_session_schema(self):
        assert doc._doctor_check_detail(_check("Session Schema", "ok", "no saved sessions for this workspace")) != ""
        assert doc._doctor_check_detail(_check("Session Schema", "warn", "legacy session data detected; x")) != ""
        assert "3" in doc._doctor_check_detail(_check("Session Schema", "ok", "3 current session file(s)"))
        assert doc._doctor_check_detail(_check("Session Schema", "ok", "other")) == "other"

    def test_provider(self):
        assert doc._doctor_check_detail(_check("Provider Environment", "warn", "no provider environment variables detected; interactive profile setup may be required")) != ""
        assert doc._doctor_check_detail(_check("Provider Environment", "ok", "OPENAI_API_KEY")) == "OPENAI_API_KEY"

    def test_policy(self):
        out = doc._doctor_check_detail(_check("Permission Policy", "ok", "default=ask, tools=3"))
        assert "ask" in out and "3" in out
        assert doc._doctor_check_detail(_check("Permission Policy", "ok", "other")) == "other"

    def test_mcp(self):
        assert doc._doctor_check_detail(_check("MCP Config", "ok", "no project .mcp.json")) != ""
        assert doc._doctor_check_detail(_check("MCP Config", "fail", "mcpServers must be a JSON object")) != ""
        assert "2" in doc._doctor_check_detail(_check("MCP Config", "ok", "2 server(s), workspace trusted"))
        assert "2" in doc._doctor_check_detail(_check("MCP Config", "warn", "2 server(s), run /mcp trust before launching project MCP servers"))
        assert doc._doctor_check_detail(_check("MCP Config", "ok", "other")) == "other"

    def test_release(self):
        assert doc._doctor_check_detail(_check("Release Gate", "ok", "scripts/check_release.py exists")) != ""
        assert doc._doctor_check_detail(_check("Release Gate", "warn", "scripts/check_release.py not found in this workspace")) != ""
        assert doc._doctor_check_detail(_check("Other", "ok", "raw")) == "raw"

    def test_render(self, tmp_path):
        checks = doc.run_doctor_checks(tmp_path)
        assert len(checks) == 13
        assert any("Risk Surface" in str(check) for check in checks)
        assert "Python" in doc.render_doctor_report(checks)
        data = json.loads(doc.render_doctor_json(checks))
        assert "ok" in data and len(data["checks"]) == 13
        assert isinstance(doc.has_failed_checks(checks), bool)


class TestChecks:
    def test_python_old(self, monkeypatch):
        monkeypatch.setattr(sys, "version_info", (3, 10, 0, "final", 0))
        c = doc._check_python()
        assert c.status == "fail"

    def test_package_variants(self, monkeypatch):
        import importlib.metadata as _md

        monkeypatch.setattr(_md, "version", lambda name: (_ for _ in ()).throw(_md.PackageNotFoundError()))
        monkeypatch.setattr(doc, "_read_source_version", lambda: "1.0")
        assert doc._check_package_metadata().status == "warn"
        monkeypatch.setattr(doc, "_read_source_version", lambda: None)
        assert "source checkout" in doc._check_package_metadata().detail
        monkeypatch.setattr(_md, "version", lambda name: "2.0")
        monkeypatch.setattr(doc, "_read_source_version", lambda: "1.0")
        c = doc._check_package_metadata()
        assert c.status == "ok" and "2.0" in c.detail
        monkeypatch.setattr(doc, "_read_source_version", lambda: "2.0")
        assert "2.0" in doc._check_package_metadata().detail

    def test_source_version(self, monkeypatch):
        from pathlib import Path as _Path

        assert doc._read_source_version() is None or isinstance(doc._read_source_version(), str)
        monkeypatch.setattr(_Path, "read_text",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
        assert doc._read_source_version() is None
        monkeypatch.setattr(_Path, "read_text", lambda *a, **k: "no version here")
        assert doc._read_source_version() is None

    def test_workspace_variants(self, tmp_path):
        assert doc._check_workspace(tmp_path / "nope").status == "fail"
        f = tmp_path / "f.txt"
        f.write_text("x", encoding="utf-8")
        assert doc._check_workspace(f).status == "fail"
        assert doc._check_workspace(tmp_path).status == "ok"

    def test_workspace_not_writable(self, tmp_path, monkeypatch):
        import tempfile as _tf

        monkeypatch.setattr(_tf, "NamedTemporaryFile",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
        assert doc._check_workspace(tmp_path).status == "fail"

    def test_git_variants(self, monkeypatch):
        import shutil as _sh
        import subprocess as _sp

        monkeypatch.setattr(_sh, "which", lambda name: None)
        assert "not found" in doc._check_git_available().detail
        monkeypatch.setattr(_sh, "which", lambda name: "/usr/bin/git")
        monkeypatch.setattr(_sp, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert "could not run" in doc._check_git_available().detail

        from types import SimpleNamespace as _NS
        monkeypatch.setattr(_sp, "run", lambda *a, **k: _NS(stdout="", stderr="", returncode=1))
        assert "non-zero" in doc._check_git_available().detail
        assert doc._check_git_available().status == "warn"

    def test_git_repo_variants(self, tmp_path, monkeypatch):
        import shutil as _sh
        import subprocess as _sp

        monkeypatch.setattr(_sh, "which", lambda name: None)
        assert doc._check_git_repository(tmp_path).detail == "skipped"
        assert doc._check_git_repository(tmp_path / "nope").detail == "skipped"
        monkeypatch.undo()
        _sp.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        c = doc._check_git_repository(tmp_path)
        assert c.status == "ok" and "work tree" in c.detail
        (tmp_path / ".git").rename(tmp_path / ".git-bak")
        try:
            assert "not a Git repository" in doc._check_git_repository(tmp_path).detail
        finally:
            (tmp_path / ".git-bak").rename(tmp_path / ".git")

    def test_config_dir_fail(self, monkeypatch):
        import lib.core.doctor as _doc

        monkeypatch.setattr(_doc, "ensure_private_dir",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
        assert doc._check_config_dir().status == "fail"

    def test_config_schema_variants(self, tmp_path, monkeypatch):
        from lib.core.paths import SayacodePaths

        (tmp_path / "u.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        monkeypatch.setattr(SayacodePaths, "user_config", property(lambda self: tmp_path / "u.json"))
        assert doc._check_config_schema().status == "warn"
        (tmp_path / "u.json").write_text("{broken", encoding="utf-8")
        assert doc._check_config_schema().status == "fail"
        (tmp_path / "u.json").write_text("[1]", encoding="utf-8")
        assert doc._check_config_schema().status == "fail"

    def test_session_schema_variants(self, tmp_path):
        from lib.core.paths import SayacodePaths

        state_dir = SayacodePaths.resolve(create=False).workspace_state_dir(tmp_path)
        assert "no saved" in doc._check_session_schema(tmp_path).detail
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "session.json").write_text("{broken", encoding="utf-8")
        assert doc._check_session_schema(tmp_path).status == "fail"
        (state_dir / "session.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        assert doc._check_session_schema(tmp_path).status == "warn"
        (state_dir / "session.json").write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
        sub = state_dir / "sessions" / "abc"
        sub.mkdir(parents=True)
        (sub / "session.json").write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
        c = doc._check_session_schema(tmp_path)
        assert c.status == "ok" and "2 current" in c.detail

    def test_provider_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        assert doc._check_provider_environment().status == "ok"
        for v in doc.PROVIDER_ENV_VARS:
            monkeypatch.delenv(v, raising=False)
        assert "no provider" in doc._check_provider_environment().detail

    def test_policy_variants(self, tmp_path):
        from lib.core.paths import SayacodePaths

        home = SayacodePaths.resolve(create=False)
        home.home.mkdir(parents=True, exist_ok=True)
        (home.user_permissions).write_text("{broken", encoding="utf-8")
        assert doc._check_permission_policy(tmp_path).status == "fail"
        (home.user_permissions).write_text("[1]", encoding="utf-8")
        assert doc._check_permission_policy(tmp_path).status == "fail"
        (home.user_permissions).unlink()
        assert doc._check_permission_policy(tmp_path).status == "ok"

    def test_mcp_variants(self, tmp_path):
        from lib.core.mcp_runtime import trust_mcp_workspace

        assert "no project" in doc._check_mcp_config(tmp_path).detail
        (tmp_path / ".mcp.json").write_text("{broken", encoding="utf-8")
        assert doc._check_mcp_config(tmp_path).status == "fail"
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": "oops"}), encoding="utf-8")
        assert "must be a JSON object" in doc._check_mcp_config(tmp_path).detail
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"s": {}}}), encoding="utf-8")
        assert "trust" in doc._check_mcp_config(tmp_path).detail
        trust_mcp_workspace(tmp_path)
        assert "trusted" in doc._check_mcp_config(tmp_path).detail

    def test_release(self, tmp_path):
        assert "not found" in doc._check_release_script(tmp_path).detail
        d = tmp_path / "scripts"
        d.mkdir()
        (d / "check_release.py").write_text("x", encoding="utf-8")
        assert "exists" in doc._check_release_script(tmp_path).detail

    def test_bundle(self, tmp_path):
        from lib.core.doctor import build_support_bundle, write_support_bundle

        payload = build_support_bundle(workspace=tmp_path)
        assert payload["workspace"] == str(tmp_path.resolve())
        out = write_support_bundle(tmp_path / "b", workspace=tmp_path)
        assert out.name == "sayacode-support-bundle.json"
        out = write_support_bundle(tmp_path, workspace=tmp_path)
        assert out.name == "sayacode-support-bundle.json"
