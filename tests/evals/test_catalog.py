"""冻结输入和源码归档不能悄悄换题，也不能包含本地凭据。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from evals import catalog, run


def make_catalog(root: Path, identifier: str = "sample-task") -> Path:
    (root / "cases").mkdir()
    (root / "fixtures" / "sample").mkdir(parents=True)
    fixture = root / "fixtures" / "sample" / "app.py"
    fixture.write_text("value = 1\n", encoding="utf-8")
    cases = root / "cases" / "dev.jsonl"
    cases.write_text(
        json.dumps(
            {
                "id": identifier,
                "split": "dev",
                "category": "bugfix",
                "fixture": "sample",
                "prompt": "修复边界",
                "checks": [{"type": "python", "code": "assert False"}],
            }
        ),
        encoding="utf-8",
    )
    (root / "cases" / "freeze.json").write_text(
        json.dumps(
            {
                "sha256": {
                    "cases/dev.jsonl": catalog.digest(cases),
                    "fixtures/sample/app.py": catalog.digest(fixture),
                }
            }
        ),
        encoding="utf-8",
    )
    return cases


def test_fixture_change_invalidates_default_and_specialized_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = make_catalog(tmp_path)
    monkeypatch.setattr(catalog, "ROOT", tmp_path)
    assert len(catalog.load_cases()) == 1
    (tmp_path / "fixtures" / "sample" / "app.py").write_text("value = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="冻结任务被修改"):
        catalog.load_cases()
    with pytest.raises(ValueError, match="冻结任务被修改"):
        catalog.load_cases(cases)


def test_case_identity_cannot_escape_results_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_catalog(tmp_path, "../../escape")
    monkeypatch.setattr(catalog, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="任务 ID"):
        catalog.load_cases()


def test_source_archive_excludes_profiles_credentials_and_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    eval_root = root / "evals"
    eval_root.mkdir(parents=True)
    (root / "src" / "sayacode").mkdir(parents=True)
    (root / "src" / "sayacode" / "app.py").write_text("value = 1", encoding="utf-8")
    for name in ("pyproject.toml", "uv.lock"):
        (root / name).write_text("", encoding="utf-8")
    (eval_root / "Dockerfile").write_text("FROM example", encoding="utf-8")
    (eval_root / "profiles").mkdir()
    (eval_root / "profiles" / "private.json").write_text('{"api_key":"private"}', encoding="utf-8")
    (root / ".env").write_text("secret=private", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr(run, "ROOT", eval_root)
    assert run.archive_source(output) == catalog.digest(output / "source.zip")
    with zipfile.ZipFile(output / "source.zip") as archive:
        assert set(archive.namelist()) == {
            "src/sayacode/app.py",
            "evals/Dockerfile",
            "pyproject.toml",
            "uv.lock",
        }
