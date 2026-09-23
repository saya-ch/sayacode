"""记忆提取材料不得复用完整文件输出或已知凭据。"""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sayacode.memory.evidence import TurnEvidence
from sayacode.memory.privacy import contains_secret, extraction_evidence, redact_secrets


def test_extraction_removes_sensitive_file_and_redacts_known_key() -> None:
    user = HumanMessage(content="请记住用中文注释；api_key=hidden-token", id="user-1")
    secret_file = ToolMessage(
        content="PASSWORD=stolen", name="read_file", tool_call_id="call-1", id="tool-1"
    )
    normal_file = ToolMessage(
        content="代码约定：中文注释。\n" + "x" * 6000,
        name="read_file",
        tool_call_id="call-2",
        id="tool-2",
    )
    answer = AIMessage(content="配置请用中文；Bearer verylongtokenvalue", id="answer-1")
    evidence = TurnEvidence(
        messages=(user, secret_file, normal_file, answer),
        source_ids=("user-1", "tool-1", "tool-2", "answer-1"),
        verified_source_ids=("tool-1", "tool-2"),
        tool_calls={
            "call-1": {"args": {"path": ".env"}},
            "call-2": {"args": {"path": "README.md"}},
        },
    )
    safe = extraction_evidence(evidence, ("hidden-token",))
    assert safe.source_ids == ("user-1", "tool-2", "answer-1")
    assert safe.verified_source_ids == ("tool-2",)
    text = "\n".join(str(message.content) for message in safe.messages)
    assert "hidden-token" not in text
    assert "stolen" not in text
    assert "verylongtokenvalue" not in text
    assert "后续内容已省略" in text


def test_long_term_memory_rejects_credential_values() -> None:
    assert contains_secret("API_KEY=abc")
    assert contains_secret("Bearer verylongtokenvalue")
    assert contains_secret("use this credential", ("credential",))
    assert not contains_secret("密码策略需要至少 12 个字符")


def test_shell_evidence_keeps_exit_status_but_not_raw_output() -> None:
    tool = ToolMessage(
        content=json.dumps(
            {
                "exit_code": 0,
                "timed_out": False,
                "stdout": "unrecognized-private-value-123456",
                "stderr": "",
                "stdout_bytes": 33,
                "stderr_bytes": 0,
            }
        ),
        name="execute_command_tool",
        tool_call_id="shell-1",
        id="tool-shell-1",
    )
    evidence = TurnEvidence((tool,), ("tool-shell-1",), (), {})
    safe = extraction_evidence(evidence)
    assert len(safe.messages) == 1
    assert '"exit_code": 0' in str(safe.messages[0].content)
    assert "unrecognized-private-value" not in str(safe.messages[0].content)


def test_url_aws_and_partial_private_key_are_removed_before_extra_model_request() -> None:
    samples = (
        "https://alice:supersecret@example.test/repo.git",
        "AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP",
        "AWS_SECRET_ACCESS_KEY=example-secret",
        "https://host.test/file?X-Amz-Signature=abcdef",
        "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        "-----BEGIN OPENSSH PRIVATE KEY-----\npartial-content",
    )
    for sample in samples:
        assert contains_secret(sample)
        assert sample not in redact_secrets(sample)
