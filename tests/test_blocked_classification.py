# 拒绝判定不能靠正文子串：读一份含拒绝字样的文件不是权限拒绝。

from langchain_core.messages import ToolMessage

from lib.tools import _tool_result_was_blocked

MARKERS = ("Permission required for tool", "Permission denied for tool", "安全检查失败",
           "安全警告", "危险操作已阻止", "工作目录不安全", "操作已中止", "Hook '")


class TestRealDenials:
    def test_warn_prefixed_messages_are_blocked(self):
        assert _tool_result_was_blocked("⚠️ Permission denied for tool 'x' by session policy.")
        assert _tool_result_was_blocked("⚠️ 工作目录不安全: 越界")
        assert _tool_result_was_blocked("⚠️ 安全检查失败: 危险命令")
        assert _tool_result_was_blocked("⚠️ Hook 'guard' blocked PreToolUse: 命中规则")
        assert _tool_result_was_blocked("⚠️ 🔴 危险操作已阻止: 系统文件")
        assert _tool_result_was_blocked("⚠️ 安全警告: 受保护路径")


class TestFalsePositives:
    def test_file_content_mentioning_markers_is_not_blocked(self):
        """回归：读到的源码里含这些字样（项目自己的 file_tools.py 就有）不是拒绝。"""
        text = ('📄 文件: lib/tools/file_tools.py\n\n    return f"⚠️ 安全警告: {reason}"')
        # 旧判据（全文子串匹配）会把它当成权限拒绝。
        assert any(marker in text for marker in MARKERS) is True
        assert _tool_result_was_blocked(text) is False

    def test_marker_deep_in_the_body_is_not_blocked(self):
        text = "📄 文件: a.py\n\n" + ("x" * 500) + "\n操作已中止\n"
        assert _tool_result_was_blocked(text) is False

    def test_marker_on_a_later_line_is_not_blocked(self):
        assert _tool_result_was_blocked("⚠️ 见下文\n安全警告") is False

    def test_plain_result_is_not_blocked(self):
        assert _tool_result_was_blocked("echo:hi") is False

    def test_batch_partial_failure_report_is_not_blocked(self):
        """多行部分失败报告不是「整次调用被拒」。"""
        text = "批量编辑完成：1 成功 / 1 失败\n[2] a.py: 写入安全检查失败 - 受保护文件"
        assert _tool_result_was_blocked(text) is False


class TestShapeTolerance:
    def test_non_string_and_empty(self):
        assert _tool_result_was_blocked(None) is False
        assert _tool_result_was_blocked("") is False
        assert _tool_result_was_blocked(123) is False

    def test_tool_message_is_unwrapped(self):
        message = ToolMessage(
            content="⚠️ Permission denied for tool 'x' by session policy.",
            name="x",
            tool_call_id="c1",
        )
        assert _tool_result_was_blocked(message) is True
