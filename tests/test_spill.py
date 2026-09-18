# 工具大输出 spill：预览 + 定位符给模型，完整内容留在工作区。

from pathlib import Path

from lib.core.mcp_runtime import MCPToolInfo, _build_langchain_tool, _spill_oversized_result
from lib.core.spill import OUTPUT_DIR_NAME, preview_with_locator, spill_text
from lib.tools.file_tools import read_file, set_default_workspace

BIG = "x" * 25000


def _info(alias="mcp_s_big"):
    return MCPToolInfo(alias=alias, server_name="s", name="big", description="d",
                       input_schema={"type": "object", "properties": {}})


class TestSpillStore:
    def test_writes_inside_the_workspace(self, tmp_path):
        path = spill_text(tmp_path, "mcp_tool", "hello")
        assert path is not None and path.is_file()
        assert path.read_text(encoding="utf-8") == "hello"
        # 必须落在工作区内：read_file 强制工作区限定，落在外面的定位符读不到。
        assert str(path).startswith(str(tmp_path.resolve()))
        assert OUTPUT_DIR_NAME in path.parts

    def test_preview_keeps_prefix_and_locator(self, tmp_path):
        path = tmp_path / "f.txt"
        text = preview_with_locator("y" * 100, path, 10)
        assert text.startswith("y" * 10)
        assert str(path) in text
        assert "90" in text


class TestSpillPolicy:
    def test_small_result_passes_through(self, tmp_path):
        set_default_workspace(tmp_path)
        content, artifact = _spill_oversized_result("small", "mcp_x")
        assert content == "small" and artifact == {}

    def test_large_result_is_spilled_in_full(self, tmp_path):
        set_default_workspace(tmp_path)
        content, artifact = _spill_oversized_result(BIG, "mcp_x")
        assert len(content) < len(BIG)
        assert artifact["truncated"] is True
        assert artifact["chars"] == len(BIG)
        saved = Path(artifact["spill_path"])
        assert saved.read_text(encoding="utf-8") == BIG


class TestToolContract:
    def test_tool_declares_content_and_artifact(self, tmp_path):
        tool = _build_langchain_tool(_info(), caller=lambda alias, args: BIG)
        assert tool.response_format == "content_and_artifact"

    def test_spilled_locator_is_readable_by_read_file(self, tmp_path):
        """定位符必须真的能读——这是落盘相对截断的唯一优势。"""
        set_default_workspace(tmp_path)
        tool = _build_langchain_tool(_info(), caller=lambda alias, args: BIG)
        content, artifact = tool.func()
        assert len(content) < len(BIG)
        assert artifact["truncated"] is True
        saved = Path(artifact["spill_path"])
        assert saved.read_text(encoding="utf-8") == BIG
        assert BIG in read_file.func(str(saved))
        # 模型侧只拿到预览：artifact 不进入模型视野。
        assert len(tool.invoke({})) < len(BIG)

    def test_small_tool_result_has_no_spill(self, tmp_path):
        set_default_workspace(tmp_path)
        tool = _build_langchain_tool(_info(), caller=lambda alias, args: "ok")
        content, artifact = tool.func()
        assert content == "ok" and artifact == {}
        assert "ok" in tool.invoke({})

class TestSpillFallback:
    def test_spill_text_returns_none_when_it_cannot_write(self, tmp_path, monkeypatch):
        import lib.core.spill as spill_mod

        def boom(*args, **kwargs):
            raise OSError("nope")

        monkeypatch.setattr(spill_mod, "ensure_private_dir", boom)
        assert spill_mod.spill_text(tmp_path, "src", "text") is None

    def test_spill_failure_falls_back_to_truncation(self, tmp_path, monkeypatch):
        """落盘失败不能变成新故障点：退回原来的截断行为。"""
        import lib.core.spill as spill_mod

        monkeypatch.setattr(spill_mod, "spill_text", lambda *args, **kwargs: None)
        set_default_workspace(tmp_path)
        content, artifact = _spill_oversized_result(BIG, "mcp_x")
        assert content.endswith("[truncated]")
        assert artifact == {"truncated": True}
