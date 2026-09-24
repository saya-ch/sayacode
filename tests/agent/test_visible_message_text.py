"""公开消息投影只展示助手正文，不把推理块混进页面或最终回答。"""

from langchain_core.messages import AIMessage

from sayacode.agent.events import _final_text, _message_text


def test_assistant_reasoning_block_is_not_public_text() -> None:
    message = AIMessage(
        content=[
            {"type": "reasoning", "text": "private chain"},
            {"type": "thinking", "text": "private thoughts"},
            {"type": "text", "text": "公开回答"},
        ]
    )
    assert _message_text(message) == "公开回答"
    assert _final_text({"messages": [message]}) == "公开回答"
