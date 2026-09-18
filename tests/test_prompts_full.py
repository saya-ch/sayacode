# prompts 全覆盖：全部人格、提醒变体、规范化。

import pytest

from lib.prompts import (
    get_concise_prompt,
    get_genki_prompt,
    get_mesugaki_prompt,
    get_system_prompt,
    get_tsundere_prompt,
    list_prompt_styles,
    normalize_prompt_style,
    prompt_style_label,
)
from lib.prompts.reminders import get_system_reminders


class TestStyles:
    @pytest.mark.parametrize("style", list(list_prompt_styles()))
    def test_every_style_builds(self, style):
        from lib.prompts.system_prompt import get_prompt_by_style

        out = get_prompt_by_style(style=style, workspace="/tmp", project_summary="demo")
        assert isinstance(out, str) and len(out) > 50

    def test_unknown_style_falls_back(self):
        from lib.prompts.system_prompt import get_prompt_by_style

        out = get_prompt_by_style(style="ghost-style")
        assert isinstance(out, str) and len(out) > 50

    def test_concise_extras(self):
        out = get_concise_prompt(project_summary="demo", workspace="/tmp")
        assert "demo" in out and "/tmp" in out

    def test_individual(self):
        assert "SAYA" in get_tsundere_prompt()
        assert "SAYA" in get_genki_prompt()
        assert "SAYA" in get_mesugaki_prompt()
        assert "SAYA" in get_concise_prompt()
        assert "SAYA" in get_system_prompt()

    def test_overlay_unknown(self):
        from lib.prompts.fragments.personality_overlay import build_personality_overlay

        assert build_personality_overlay("ghost", "SAYA") == ""
        assert build_personality_overlay("tsundere", "SAYA") != ""


class TestNormalize:
    def test_variants(self):
        assert normalize_prompt_style(None) == "standard"
        assert normalize_prompt_style("") == "standard"
        assert normalize_prompt_style("CONCISE") == "concise"
        assert normalize_prompt_style("onee_san") == "onee-san"
        assert normalize_prompt_style("neko") == "catgirl"
        assert normalize_prompt_style("ghost", fallback=None) is None
        assert normalize_prompt_style("ghost") == "standard"

    def test_labels(self):
        assert prompt_style_label(None) != ""
        assert prompt_style_label("concise") != ""
        assert prompt_style_label("ghost") == prompt_style_label("standard")

    def test_raw_alias_defense(self, monkeypatch):
        import lib.prompts.system_prompt as _sp

        monkeypatch.setitem(_sp.PROMPT_STYLE_ALIASES, "Weird_Key", "concise")
        assert normalize_prompt_style("Weird_Key") == "concise"

    def test_lower_alias_defense(self, monkeypatch):
        import lib.prompts.system_prompt as _sp

        monkeypatch.setitem(_sp.PROMPT_STYLE_ALIASES, "weird_2", "concise")
        assert normalize_prompt_style("WEIRD_2") == "concise"

    def test_modes_and_fallbacks(self, monkeypatch):
        import lib.prompts.system_prompt as _sp

        assert "plan" in _sp.get_system_prompt(agent_mode="plan").lower() or "Plan" in _sp.get_system_prompt(agent_mode="plan")
        assert _sp.get_system_prompt(agent_mode="review") != ""
        monkeypatch.setattr(_sp, "build_personality_overlay", lambda *a, **k: "")
        for fn in (_sp.get_tsundere_prompt, _sp.get_genki_prompt, _sp.get_mesugaki_prompt,
                   _sp.get_onee_san_prompt, _sp.get_idol_prompt, _sp.get_catgirl_prompt,
                   _sp.get_mukuchi_prompt):
            assert isinstance(fn(), str)

    def test_overlay_standard_empty(self):
        from lib.prompts.fragments.personality_overlay import build_personality_overlay

        assert build_personality_overlay("standard", "SAYA") == ""
        assert build_personality_overlay("", "SAYA") == ""


class TestReminders:
    def test_empty(self):
        assert get_system_reminders() == ""
        assert get_system_reminders({}) == ""
        assert get_system_reminders({"agent_mode": "build"}) == ""

    def test_plan(self):
        assert "Plan" in get_system_reminders({"agent_mode": "plan"})

    def test_review(self):
        assert "Review" in get_system_reminders({"agent_mode": "review"})

    def test_urgent(self):
        out = get_system_reminders({"context_usage": 0.9})
        assert "紧急" in out

    def test_high(self):
        out = get_system_reminders({"context_usage": 0.75})
        assert "偏高" in out

    def test_non_numeric_usage(self):
        assert get_system_reminders({"context_usage": "oops"}) == ""

    def test_language(self):
        assert "中文" in get_system_reminders({"language": "zh-CN"})
