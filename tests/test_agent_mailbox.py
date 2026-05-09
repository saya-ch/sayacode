"""P3: AgentMailbox 测试."""

import json
import tempfile
from pathlib import Path

import pytest
from lib.core.agent_mailbox import AgentMailbox, MailboxMessage


class TestAgentMailbox:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    @pytest.fixture
    def mailbox(self, tmp_dir):
        return AgentMailbox(tmp_dir, "test_agent")

    def test_directory_created(self, tmp_dir):
        mailbox = AgentMailbox(tmp_dir, "worker_1")
        assert mailbox.mailbox_dir.exists()
        assert mailbox.mailbox_dir.is_dir()

    def test_write_creates_file(self, mailbox):
        path = mailbox.write({"type": "task", "task": "analyze"})
        assert path.exists()
        assert path.suffix == ".json"

    def test_write_content_roundtrip(self, mailbox):
        mailbox.write({"type": "task", "payload": {"file": "test.py"}}, sender="leader")
        messages = mailbox.read_all()
        assert len(messages) == 1
        assert messages[0].content["type"] == "task"
        assert messages[0].content["payload"]["file"] == "test.py"
        assert messages[0].sender == "leader"

    def test_read_unread_skips_read(self, mailbox):
        mailbox.write({"msg": 1})
        mailbox.write({"msg": 2})
        messages = mailbox.read_unread()
        assert len(messages) == 2

        mailbox.mark_read(messages[0].message_id)
        unread = mailbox.read_unread()
        assert len(unread) == 1
        # 剩余未读消息的 msg 值取决于 UUID 排序，不检查具体值

    def test_read_all_includes_read(self, mailbox):
        mailbox.write({"msg": 1})
        msgs = mailbox.read_all()
        mailbox.mark_read(msgs[0].message_id)

        all_msgs = mailbox.read_all(include_read=True)
        assert len(all_msgs) == 1
        assert all_msgs[0].is_read

    def test_mark_read_nonexistent(self, mailbox):
        assert not mailbox.mark_read("nonexistent_id")

    def test_mark_read_returns_true(self, mailbox):
        mailbox.write({"test": True})
        msgs = mailbox.read_all()
        assert mailbox.mark_read(msgs[0].message_id)

    def test_message_count(self, mailbox):
        assert mailbox.message_count == 0
        mailbox.write({"msg": 1})
        assert mailbox.message_count == 1
        mailbox.write({"msg": 2})
        assert mailbox.message_count == 2

    def test_unread_count(self, mailbox):
        assert mailbox.unread_count == 0
        mailbox.write({"msg": 1})
        assert mailbox.unread_count == 1
        msgs = mailbox.read_all()
        mailbox.mark_read(msgs[0].message_id)
        assert mailbox.unread_count == 0

    def test_clear(self, mailbox):
        mailbox.write({"msg": 1})
        mailbox.write({"msg": 2})
        count = mailbox.clear()
        assert count == 2
        assert mailbox.message_count == 0

    def test_multiple_agents_separate_mailboxes(self, tmp_dir):
        mb_a = AgentMailbox(tmp_dir, "agent_a")
        mb_b = AgentMailbox(tmp_dir, "agent_b")

        mb_a.write({"to": "a"})
        mb_b.write({"to": "b"})

        # 写信箱互不干扰
        assert len(mb_a.read_all()) == 1
        assert len(mb_b.read_all()) == 1
        assert mb_a.read_all()[0].content["to"] == "a"
        assert mb_b.read_all()[0].content["to"] == "b"

    def test_mailbox_message_dataclass(self):
        msg = MailboxMessage(
            message_id="abc123",
            sender="leader",
            content={"cmd": "review"},
            timestamp=12345.0,
            is_read=False,
        )
        assert msg.message_id == "abc123"
        assert msg.sender == "leader"
        assert not msg.is_read

    def test_sort_order_is_lexicographic(self, mailbox):
        """消息应按文件名排序（= 按 UUID 排序）。"""
        mailbox.write({"order": 1})
        mailbox.write({"order": 2})
        mailbox.write({"order": 3})
        msgs = mailbox.read_all()
        assert len(msgs) == 3
        # 时间戳可能相同，但文件名按 UUID 排序保证确定性
        ids = [m.message_id for m in msgs]
        assert ids == sorted(ids)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
