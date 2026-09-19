# custom_commands 全覆盖：发现、渲染、mcp 配置。

import json


from lib.commands.custom import (
    CustomCommand,
    discover_custom_commands,
    list_custom_commands,
    load_project_mcp_config,
    render_custom_command,
)


def _md(root, rel, body):
    # 写一个 markdown 命令文件。
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


class TestProps:
    def test_invocations(self, tmp_path):
        c = CustomCommand(name="deploy", path=tmp_path, scope="project", body="b")
        assert c.primary_invocation == "/deploy"
        assert c.qualified_invocation is None
        assert c.invocations == ("/deploy",)
        assert c.source_label == "project"
        c2 = CustomCommand(name="d", path=tmp_path, scope="project", body="b", namespace="sub")
        assert c2.qualified_invocation == "/sub:d"
        assert c2.invocations == ("/d", "/sub:d")
        assert c2.source_label == "project:sub"


class TestFrontmatter:
    def test_all_forms(self, tmp_path):
        from lib.commands.custom import _split_frontmatter

        assert _split_frontmatter("body") == ({}, "body")
        assert _split_frontmatter("---\nno-close") == ({}, "---\nno-close")
        meta, body = _split_frontmatter('---\ndescription: "hi"\nbad-line\nkey: \'v\'\n---\nrest')
        assert meta == {"description": "hi", "key": "v"} and body == "rest"


class TestDiscover:
    def test_project_and_nested(self, tmp_path, monkeypatch):
        import pathlib as _pl

        monkeypatch.setattr(_pl.Path, "home", lambda: tmp_path / "home-nope")
        _md(tmp_path / ".claude" / "commands", "deploy.md", "---\ndescription: Deploy\n---\nRun $ARGUMENTS")
        _md(tmp_path / ".claude" / "commands", "sub/deep.md", "Deep $1 and $2, drop $9")
        found = discover_custom_commands(tmp_path)
        assert "/deploy" in found and "/sub:deep" in found
        assert found["/deploy"].description == "Deploy"

    def test_unreadable_skipped(self, tmp_path, monkeypatch):
        import pathlib as _pl
        from pathlib import Path as _Path

        monkeypatch.setattr(_pl.Path, "home", lambda: tmp_path / "home-nope")
        _md(tmp_path / ".claude" / "commands", "ok.md", "body")
        _md(tmp_path / ".claude" / "commands", "bad.md", "body")
        real_read = _Path.read_text

        def flaky(self, *args, **kwargs):
            if self.name == "bad.md":
                raise OSError("busy")
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(_Path, "read_text", flaky)
        found = discover_custom_commands(tmp_path)
        assert "/ok" in found and "/bad" not in found

    def test_empty_name_skipped(self, tmp_path, monkeypatch):
        import pathlib as _pl

        monkeypatch.setattr(_pl.Path, "home", lambda: tmp_path / "home-nope")
        _md(tmp_path / ".claude" / "commands", "   .md", "body")
        assert discover_custom_commands(tmp_path) == {}

    def test_user_scope(self, tmp_path):
        home = tmp_path / "home"
        import pathlib as _pl
        from unittest import mock

        with mock.patch.object(_pl.Path, "home", return_value=home):
            _md(home / ".claude" / "commands", "u.md", "user body")
            found = discover_custom_commands(tmp_path)
        assert "/u" in found and found["/u"].scope == "user"

    def test_list_sorted_dedup(self, tmp_path, monkeypatch):
        import pathlib as _pl

        monkeypatch.setattr(_pl.Path, "home", lambda: tmp_path / "home-nope")
        _md(tmp_path / ".claude" / "commands", "b.md", "b")
        _md(tmp_path / ".claude" / "commands", "a.md", "a")
        listed = list_custom_commands(tmp_path)
        assert [c.name for c in listed] == ["a", "b"]


class TestRender:
    def _ws(self, tmp_path, monkeypatch):
        import pathlib as _pl

        monkeypatch.setattr(_pl.Path, "home", lambda: tmp_path / "home-nope")
        _md(tmp_path / ".claude" / "commands", "greet.md", "Hello $1! Args: $ARGUMENTS ($9)")
        return tmp_path

    def test_basic(self, tmp_path, monkeypatch):
        ws = self._ws(tmp_path, monkeypatch)
        cmd, out = render_custom_command("/greet Bob", ws)
        assert cmd.name == "greet" and out == "Hello Bob! Args: Bob ()"

    def test_no_slash(self, tmp_path, monkeypatch):
        ws = self._ws(tmp_path, monkeypatch)
        assert render_custom_command("greet", ws) == (None, None)

    def test_unknown(self, tmp_path, monkeypatch):
        ws = self._ws(tmp_path, monkeypatch)
        assert render_custom_command("/ghost", ws) == (None, None)

    def test_no_args(self, tmp_path, monkeypatch):
        ws = self._ws(tmp_path, monkeypatch)
        _, out = render_custom_command("/greet", ws)
        assert out == "Hello ! Args:  ()"

    def test_bad_quoting(self, tmp_path, monkeypatch):
        ws = self._ws(tmp_path, monkeypatch)
        _, out = render_custom_command('/greet "oops', ws)
        assert "oops" in out


class TestMcpConfig:
    def test_missing(self, tmp_path):
        path, cfg = load_project_mcp_config(tmp_path)
        assert cfg == {} and path.name == ".mcp.json"

    def test_ok(self, tmp_path):
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        _, cfg = load_project_mcp_config(tmp_path)
        assert cfg == {"mcpServers": {}}

    def test_broken(self, tmp_path):
        (tmp_path / ".mcp.json").write_text("{broken", encoding="utf-8")
        _, cfg = load_project_mcp_config(tmp_path)
        assert cfg == {}
