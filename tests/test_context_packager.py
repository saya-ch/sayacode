from lib.core.context import ProjectContext
from lib.core.context_packager import ContextPackRequest, ContextPackager, TokenEstimate


def test_context_packager_includes_project_and_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    (tmp_path / "SAYACODE.md").write_text("Use pytest for tests.", encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    package = ContextPackager().pack(ContextPackRequest(
        workspace=tmp_path,
        project_context=ProjectContext(str(tmp_path)),
        max_chars=12000,
    ))

    assert "项目:" in package.content
    assert "Persistent Memory" in package.content
    assert "Use pytest for tests." in package.content
    assert package.truncated is False


def test_context_packager_allows_workspace_memory_import(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    (tmp_path / "notes.md").write_text("Use ruff.", encoding="utf-8")
    (tmp_path / "SAYACODE.md").write_text("@./notes.md", encoding="utf-8")

    package = ContextPackager().pack(ContextPackRequest(
        workspace=tmp_path,
        include_project=False,
        max_chars=12000,
    ))

    assert "Use ruff." in package.content


def test_context_packager_blocks_memory_import_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    outside = tmp_path.parent / f"{tmp_path.name}_secret.txt"
    outside.write_text("SECRET=leaked", encoding="utf-8")
    (tmp_path / "SAYACODE.md").write_text(f"@{outside.as_posix()}", encoding="utf-8")

    package = ContextPackager().pack(ContextPackRequest(
        workspace=tmp_path,
        include_project=False,
        max_chars=12000,
    ))

    assert "SECRET=leaked" not in package.content
    assert "blocked memory import" in package.content


def test_context_packager_respects_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    (tmp_path / "SAYACODE.md").write_text("x" * 1000, encoding="utf-8")

    package = ContextPackager().pack(ContextPackRequest(
        workspace=tmp_path,
        include_project=False,
        max_chars=120,
    ))

    assert len(package.content) <= 140
    assert package.truncated is True


def test_context_packager_explain_marks_estimated_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    (tmp_path / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    package = ContextPackager().pack(ContextPackRequest(
        workspace=tmp_path,
        include_project=False,
        include_memory=False,
        include_symbols=True,
        max_chars=12000,
    ))

    assert "symbols" in package.included_sections
    assert package.estimated_tokens > 0
    assert package.token_estimate_is_exact is False
    assert any(item["section"] == "symbols" for item in package.explain)


def test_context_packager_accepts_provider_token_estimator(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))

    class ExactEstimator:
        def estimate(self, text):
            return TokenEstimate(tokens=7 if text else 0, exact=True)

    explanation = ContextPackager().explain(ContextPackRequest(
        workspace=tmp_path,
        include_project=False,
        include_memory=True,
        include_history=False,
        token_estimator=ExactEstimator(),
        max_chars=12000,
    ))

    assert explanation["token_estimate_is_exact"] is True
