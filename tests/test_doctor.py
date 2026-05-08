import json

from lib.core.doctor import has_failed_checks, render_doctor_json, render_doctor_report, run_doctor_checks
from lib.i18n import get_language_preference, set_language


def test_doctor_renders_core_checks(tmp_path):
    previous_language = get_language_preference()
    set_language("en")
    try:
        checks = run_doctor_checks(tmp_path)
        report = render_doctor_report(checks)
    finally:
        set_language(previous_language)

    assert "SAYACODE Doctor" in report
    assert "Python" in report
    assert "Workspace" in report


def test_doctor_report_respects_language(tmp_path):
    previous_language = get_language_preference()
    set_language("zh")
    try:
        report = render_doctor_report(run_doctor_checks(tmp_path))
    finally:
        set_language(previous_language)

    assert "SAYACODE 诊断" in report
    assert "工作区" in report
    assert "可写" in report
    assert "not installed as a package" not in report
    assert "[通过]" in report or "[警告]" in report or "[失败]" in report


def test_doctor_reports_missing_workspace_as_failure(tmp_path):
    checks = run_doctor_checks(tmp_path / "missing")

    assert has_failed_checks(checks)


def test_doctor_json_is_machine_readable(tmp_path):
    payload = json.loads(render_doctor_json(run_doctor_checks(tmp_path)))

    assert payload["ok"] in {True, False}
    assert any(check["name"] == "Python" for check in payload["checks"])
