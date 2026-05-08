from lib.tools.safety import check_file_danger, sanitize_path
from lib.core.context import ProjectContext


def test_blocks_secret_key_files(tmp_path):
    secret_file = tmp_path / "id_rsa"
    secret_file.write_text("PRIVATE KEY", encoding="utf-8")

    is_safe, reason = check_file_danger(str(secret_file))

    assert not is_safe
    assert "敏感" in reason


def test_allows_env_example_templates(tmp_path):
    template_file = tmp_path / ".env.example"
    template_file.write_text("OPENAI_API_KEY=", encoding="utf-8")

    is_safe, reason = check_file_danger(str(template_file))

    assert is_safe, reason


def test_sanitize_path_blocks_sensitive_files_inside_workspace(tmp_path):
    try:
        sanitize_path("id_rsa", base_dir=tmp_path)
    except ValueError as exc:
        assert "敏感" in str(exc)
    else:
        raise AssertionError("sanitize_path should block sensitive files")


def test_sanitize_path_still_allows_normal_workspace_files(tmp_path):
    result = sanitize_path("README.md", base_dir=tmp_path)

    assert result == tmp_path.resolve() / "README.md"


def test_project_context_excludes_secret_files(tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=secret", encoding="utf-8")
    (tmp_path / "main.py").write_text("print('ok')", encoding="utf-8")

    context = ProjectContext(str(tmp_path))
    paths = {file_info.path for file_info in context.files}

    assert "main.py" in paths
    assert ".env" not in paths
