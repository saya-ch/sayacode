from lib.theme import SayacodeColors, _build_summary_panel


def test_session_borders_use_soft_pink_theme():
    panel = _build_summary_panel("Session", {"Model": "test"})

    assert SayacodeColors.SESSION_BORDER == "#FFDDE8"
    assert SayacodeColors.BORDER == SayacodeColors.SESSION_BORDER
    assert SayacodeColors.BORDER_BRIGHT == SayacodeColors.SESSION_BORDER
    assert str(panel.border_style) == SayacodeColors.SESSION_BORDER


def test_user_input_border_stays_separate_from_session_border():
    assert SayacodeColors.USER_INPUT_BORDER == "#FFFFFF"
    assert SayacodeColors.USER_INPUT_BORDER != SayacodeColors.SESSION_BORDER
