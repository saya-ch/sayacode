"""P3: TeamConfig 测试."""

import json
import tempfile
from pathlib import Path

import pytest
from lib.core.team_config import TeamConfig, TeamMember


class TestTeamMember:
    def test_create_member(self):
        m = TeamMember(agent_id="a1", agent_type="code-reviewer")
        assert m.agent_id == "a1"
        assert m.agent_type == "code-reviewer"
        assert m.status == "active"

    def test_default_values(self):
        m = TeamMember(agent_id="a1", agent_type="builder")
        assert m.session_id == ""
        assert m.worktree == ""
        assert m.pane_id == ""


class TestTeamConfig:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    def test_create_empty_team(self):
        tc = TeamConfig(team_name="my-team")
        assert tc.team_name == "my-team"
        assert tc.members == []
        assert tc.created_at != ""

    def test_add_member(self):
        tc = TeamConfig(team_name="review-team")
        tc.add_member(TeamMember(agent_id="r1", agent_type="reviewer"))
        assert tc.member_count == 1
        assert tc.get_member("r1") is not None

    def test_add_duplicate_replaces(self):
        tc = TeamConfig(team_name="dup-test")
        tc.add_member(TeamMember(agent_id="x", agent_type="type-a"))
        tc.add_member(TeamMember(agent_id="x", agent_type="type-b"))
        assert tc.member_count == 1
        assert tc.get_member("x").agent_type == "type-b"

    def test_remove_member(self):
        tc = TeamConfig(team_name="rm-test")
        tc.add_member(TeamMember(agent_id="m1", agent_type="builder"))
        assert tc.remove_member("m1")
        assert tc.member_count == 0

    def test_remove_nonexistent(self):
        tc = TeamConfig(team_name="no-member")
        assert not tc.remove_member("nonexistent")

    def test_active_members(self):
        tc = TeamConfig(team_name="multi")
        tc.add_member(TeamMember(agent_id="a", agent_type="x", status="active"))
        tc.add_member(TeamMember(agent_id="b", agent_type="y", status="completed"))
        tc.add_member(TeamMember(agent_id="c", agent_type="z", status="active"))
        assert len(tc.active_members) == 2

    def test_to_dict_roundtrip(self):
        tc = TeamConfig(team_name="json-test", workspace="/tmp/proj")
        tc.add_member(TeamMember(agent_id="a1", agent_type="builder", session_id="s123"))
        d = tc.to_dict()
        assert d["team_name"] == "json-test"
        assert d["workspace"] == "/tmp/proj"
        assert len(d["members"]) == 1
        assert d["members"][0]["agent_id"] == "a1"

    def test_from_dict(self):
        d = {
            "team_name": "from-dict",
            "members": [
                {"agent_id": "m1", "agent_type": "explorer", "status": "active"}
            ],
            "created_at": "2025-01-01T00:00:00",
            "session_id": "s99",
        }
        tc = TeamConfig.from_dict(d)
        assert tc.team_name == "from-dict"
        assert tc.member_count == 1
        assert tc.get_member("m1").agent_type == "explorer"
        assert tc.session_id == "s99"

    def test_save_and_load(self, tmp_dir):
        path = tmp_dir / "config.json"
        tc = TeamConfig(team_name="persist-test")
        tc.add_member(TeamMember(agent_id="p1", agent_type="planner"))

        assert tc.save(path)

        loaded = TeamConfig.load(path)
        assert loaded is not None
        assert loaded.team_name == "persist-test"
        assert loaded.member_count == 1
        assert loaded.get_member("p1").agent_type == "planner"

    def test_load_nonexistent(self, tmp_dir):
        assert TeamConfig.load(tmp_dir / "nonexistent.json") is None

    def test_to_json_is_valid(self):
        tc = TeamConfig(team_name="json-valid")
        tc.add_member(TeamMember(agent_id="j1", agent_type="tester"))
        json_str = tc.to_json()
        data = json.loads(json_str)
        assert data["team_name"] == "json-valid"

    def test_updated_at_changes_on_save(self, tmp_dir):
        import time
        path = tmp_dir / "config.json"
        tc = TeamConfig(team_name="update-test")
        original = tc.updated_at
        time.sleep(0.01)  # 确保时间戳不同
        tc.save(path)
        assert tc.updated_at > original


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
