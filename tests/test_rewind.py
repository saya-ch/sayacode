# /rewind：图检查点与本机镜像一起回退。

from types import SimpleNamespace

from langchain_core.messages import AIMessage

from lib.commands.base import CommandContext
from lib.commands.rewind import RewindCommandHandler
from lib.core.session import SessionManager

from tests.test_agent_run import _agent


class TestSessionTruncation:
    def test_keeps_prefix_turns(self):
        session = SessionManager(session_id="s")
        session.add_user_message("q1")
        session.add_assistant_message("a1")
        session.add_user_message("q2")
        session.add_assistant_message("a2")
        assert session.truncate_to_user_turns(1) == 2
        assert [m.content for m in session.messages] == ["q1", "a1"]

    def test_zero_turns_clears_history(self):
        session = SessionManager(session_id="s")
        session.add_user_message("q1")
        session.add_assistant_message("a1")
        assert session.truncate_to_user_turns(0) == 2
        assert session.messages == []

    def test_dropping_nothing_is_a_noop(self):
        session = SessionManager(session_id="s")
        session.add_user_message("q1")
        session.add_assistant_message("a1")
        assert session.truncate_to_user_turns(5) == 0
        assert len(session.messages) == 2


class TestRewind:
    def _two_turns(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="a1"), AIMessage(content="a2"),
                                  AIMessage(content="a3")])
        assert agent.run("q1") == "a1"
        assert agent.run("q2") == "a2"
        return agent

    def test_current_turn_and_points(self, tmp_path):
        agent = self._two_turns(tmp_path)
        assert agent.runner.current_turn_count() == 2
        turns = [point["turns"] for point in agent.runner.list_rewind_points()]
        assert turns[0] == 2
        assert 1 in turns and 0 in turns

    def test_rewind_rolls_back_graph_and_session(self, tmp_path):
        agent = self._two_turns(tmp_path)
        runtime = SimpleNamespace(agent=agent)
        handler = RewindCommandHandler()
        command = CommandContext(raw="/rewind 1", name="rewind", args="1")
        assert handler.handle(command, runtime) is True
        assert agent.runner.current_turn_count() == 1
        assert [m.content for m in agent.session.messages] == ["q1", "a1"]

    def test_turn_after_rewind_continues_from_that_point(self, tmp_path):
        """回退后新的一轮接在回退点上，而不是把被回退的轮次带回来。"""
        agent = self._two_turns(tmp_path)
        runtime = SimpleNamespace(agent=agent)
        RewindCommandHandler().handle(
            CommandContext(raw="/rewind 1", name="rewind", args="1"), runtime
        )
        assert agent.run("q2b") == "a3"
        assert agent.runner.current_turn_count() == 2
        assert [m.content for m in agent.session.messages] == ["q1", "a1", "q2b", "a3"]

    def test_bad_arguments_do_not_change_state(self, tmp_path, capsys):
        agent = self._two_turns(tmp_path)
        runtime = SimpleNamespace(agent=agent)
        handler = RewindCommandHandler()
        for text, args in (("/rewind", ""), ("/rewind x", "x"), ("/rewind 0", "0"),
                           ("/rewind 9", "9")):
            assert handler.handle(
                CommandContext(raw=text, name="rewind", args=args), runtime
            ) is True
        assert agent.runner.current_turn_count() == 2
        assert len(agent.session.messages) == 4
        assert capsys.readouterr() is not None

    def test_missing_runner_is_reported(self, capsys):
        runtime = SimpleNamespace(agent=SimpleNamespace(runner=None))
        handler = RewindCommandHandler()
        assert handler.handle(
            CommandContext(raw="/rewind", name="rewind", args=""), runtime
        ) is True
        assert capsys.readouterr() is not None

class TestMemoryRollback:
    def test_interactions_roll_back_with_the_conversation(self, tmp_path):
        agent = TestRewind()._two_turns(tmp_path)
        assert len(agent.memory.interactions) == 2
        runtime = SimpleNamespace(agent=agent)
        RewindCommandHandler().handle(
            CommandContext(raw="/rewind 1", name="rewind", args="1"), runtime
        )
        assert len(agent.memory.interactions) == 1

    def test_truncate_beyond_history_is_a_noop(self):
        from lib.core.memory import MemoryManager

        memory = MemoryManager(session_id="s")
        memory.add_interaction("q", "a")
        assert memory.truncate_to_turns(5) == 0
        assert len(memory.interactions) == 1
        assert memory.truncate_to_turns(0) == 1
        assert memory.interactions == []
