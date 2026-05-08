from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _python_files(*relative_roots: str):
    for relative_root in relative_roots:
        path = ROOT / relative_root
        if path.is_file():
            yield path
        else:
            yield from path.rglob("*.py")


def test_runtime_and_commands_do_not_import_legacy_model_or_tool_globals():
    banned_tokens = (
        "ALL_TOOLS",
        "ModelFactory",
        "MODEL_REGISTRY",
        "configure_tool_workspace",
        "tool_workspace_session",
        "from lib.models.factory import",
        "from ..models.factory import",
    )

    offenders = []
    for path in _python_files("lib/runtime", "lib/commands", "lib/agent.py", "lib/__init__.py", "lib/models/__init__.py"):
        text = path.read_text(encoding="utf-8")
        for token in banned_tokens:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")

    assert offenders == []


def test_removed_compatibility_modules_do_not_exist():
    assert not (ROOT / "lib" / "models" / "factory.py").exists()


def test_removed_skill_scaffolding_stays_removed():
    offenders = []
    for path in _python_files("lib", "tests"):
        text = path.read_text(encoding="utf-8")
        for token in ("skill_manager", "_register_skills", '"/skill"', "'/skill'"):
            if token in text and path.name != "test_architecture_boundaries.py":
                offenders.append(f"{path.relative_to(ROOT)}: {token}")

    assert offenders == []


def test_tools_module_does_not_mutate_globals_for_exports():
    text = (ROOT / "lib" / "tools" / "__init__.py").read_text(encoding="utf-8")

    assert "globals()" not in text
    assert "_WRAPPED_TOOLS_BY_NAME" not in text


def test_provider_defaults_are_not_redeclared_in_runtime_surfaces():
    banned_defaults = (
        "https://api.openai.com/v1",
        "https://api.anthropic.com/v1",
        "https://generativelanguage.googleapis.com/v1beta",
        "http://localhost:11434",
        "gpt-4",
        "claude-sonnet-4-20250514",
        "gemini-2.5-flash",
        "qwen3.5:9b",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "AZURE_OPENAI_API_KEY",
    )
    offenders = []

    for path in _python_files(
        "lib/cli.py",
        "lib/runtime/model_profiles.py",
        "lib/api_config/api_config.py",
        "lib/api_config/wizard.py",
    ):
        text = path.read_text(encoding="utf-8")
        for token in banned_defaults:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")

    assert offenders == []


def test_runtime_surfaces_do_not_build_user_state_home_paths_directly():
    offenders = []
    for path in _python_files(
        "lib/runtime",
        "lib/commands",
        "lib/agent.py",
        "lib/core/doctor.py",
        "lib/core/permissions.py",
        "lib/core/hooks.py",
        "lib/core/mcp_runtime.py",
        "lib/api_config/api_config.py",
        "lib/state.py",
    ):
        text = path.read_text(encoding="utf-8")
        if 'Path.home() / ".sayacode"' in text or "Path.home() / '.sayacode'" in text:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_release_gate_uses_isolated_sayacode_home():
    text = (ROOT / "scripts" / "check_release.py").read_text(encoding="utf-8")

    assert "RELEASE_HOME" in text
    assert '"SAYACODE_HOME"' in text
    assert ".tmp_release_home" in text
