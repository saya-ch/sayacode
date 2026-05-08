"""Local diagnostic checks for installed SAYACODE environments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import platform

from .audit import read_recent_audit_events, redact_value
from .paths import SayacodePaths
from .permissions import PermissionPolicy
from .private_io import ensure_private_dir
from .mcp_runtime import is_mcp_workspace_trusted
from .session import SESSION_SCHEMA_VERSION
from ..api_config.api_config import API_CONFIG_SCHEMA_VERSION
from ..i18n import tr
from ..state import SAYACODE_CONFIG_SCHEMA_VERSION


DiagnosticStatus = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class DiagnosticCheck:
    """One doctor check result."""

    name: str
    status: DiagnosticStatus
    detail: str


PROVIDER_ENV_VARS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "OLLAMA_BASE_URL",
)


def run_doctor_checks(workspace: str | Path | None = None) -> list[DiagnosticCheck]:
    """Run local installation and workspace diagnostics."""
    workspace_path = Path(workspace).expanduser().resolve() if workspace else Path.cwd().resolve()
    checks = [
        _check_python(),
        _check_package_metadata(),
        _check_workspace(workspace_path),
        _check_git_available(),
        _check_git_repository(workspace_path),
        _check_config_dir(),
        _check_config_schema(),
        _check_session_schema(workspace_path),
        _check_provider_environment(),
        _check_permission_policy(workspace_path),
        _check_mcp_config(workspace_path),
        _check_release_script(workspace_path),
    ]
    return checks


def render_doctor_report(checks: Iterable[DiagnosticCheck]) -> str:
    """Render checks as plain terminal text."""
    lines = [tr("doctor.report_title"), ""]
    for check in checks:
        marker = {
            "ok": f"[{tr('doctor.status.ok')}]",
            "warn": f"[{tr('doctor.status.warn')}]",
            "fail": f"[{tr('doctor.status.fail')}]",
        }[check.status]
        lines.append(f"{marker} {_doctor_check_name(check.name)}: {_doctor_check_detail(check)}")
    return "\n".join(lines)


def _doctor_check_name(name: str) -> str:
    key = "doctor.check." + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    translated = tr(key)
    return name if translated == key else translated


def _doctor_check_detail(check: DiagnosticCheck) -> str:
    """Localize common doctor details for human reports without changing JSON."""
    detail = check.detail

    if check.name == "Python":
        match = re.match(r"(?P<version>[\d.]+) satisfies >= 3\.11$", detail)
        if match:
            return tr("doctor.detail.python_ok", version=match.group("version"))
        match = re.match(r"(?P<version>[\d.]+) is too old; Python >= 3\.11 is required$", detail)
        if match:
            return tr("doctor.detail.python_old", version=match.group("version"))

    if check.name == "Package":
        if detail == "not installed as a package; running from source checkout":
            return tr("doctor.detail.package_source")
        match = re.match(
            r"sayacode (?P<source>[^ ]+) \(source checkout; installed distribution (?P<installed>[^)]+)\)$",
            detail,
        )
        if match:
            return tr(
                "doctor.detail.package_source_with_installed",
                source=match.group("source"),
                installed=match.group("installed"),
            )
        return detail

    if check.name == "Workspace":
        match = re.match(r"(?P<path>.+) is writable$", detail)
        if match:
            return tr("doctor.detail.workspace_writable", path=match.group("path"))
        match = re.match(r"(?P<path>.+) does not exist$", detail)
        if match:
            return tr("doctor.detail.workspace_missing", path=match.group("path"))
        match = re.match(r"(?P<path>.+) is not a directory$", detail)
        if match:
            return tr("doctor.detail.workspace_not_directory", path=match.group("path"))
        match = re.match(r"(?P<path>.+) is not writable: (?P<error>.+)$", detail)
        if match:
            return tr("doctor.detail.workspace_not_writable", path=match.group("path"), error=match.group("error"))

    if check.name == "Git":
        if detail == "git executable was not found in PATH":
            return tr("doctor.detail.git_missing")
        if detail == "git returned a non-zero exit code":
            return tr("doctor.detail.git_nonzero")
        match = re.match(r"git exists but could not run: (?P<error>.+)$", detail)
        if match:
            return tr("doctor.detail.git_run_failed", error=match.group("error"))
        return detail

    if check.name == "Git Repository":
        if detail == "skipped":
            return tr("doctor.detail.skipped")
        match = re.match(r"(?P<path>.+) is inside a Git work tree$", detail)
        if match:
            return tr("doctor.detail.git_repo_ok", path=match.group("path"))
        if detail == "workspace is not a Git repository yet":
            return tr("doctor.detail.git_repo_missing")

    if check.name == "Config Directory":
        match = re.match(r"(?P<path>.+) is available$", detail)
        if match:
            return tr("doctor.detail.config_dir_ok", path=match.group("path"))
        match = re.match(r"(?P<path>.+) is not writable: (?P<error>.+)$", detail)
        if match:
            return tr("doctor.detail.config_dir_not_writable", path=match.group("path"), error=match.group("error"))

    if check.name == "Config Schema":
        if detail == "current config schema":
            return tr("doctor.detail.config_schema_ok")
        if detail.startswith("legacy config detected;"):
            return tr("doctor.detail.config_schema_legacy")

    if check.name == "Session Schema":
        if detail == "no saved sessions for this workspace":
            return tr("doctor.detail.session_schema_none")
        if detail.startswith("legacy session data detected;"):
            return tr("doctor.detail.session_schema_legacy")
        match = re.match(r"(?P<count>\d+) current session file\(s\)$", detail)
        if match:
            return tr("doctor.detail.session_schema_count", count=match.group("count"))

    if check.name == "Provider Environment":
        if detail == "no provider environment variables detected; interactive profile setup may be required":
            return tr("doctor.detail.provider_env_missing")

    if check.name == "Permission Policy":
        match = re.match(r"default=(?P<default>[^,]+), tools=(?P<tools>\d+)$", detail)
        if match:
            return tr(
                "doctor.detail.permission_policy_ok",
                default=match.group("default"),
                tools=match.group("tools"),
            )

    if check.name == "MCP Config":
        if detail == "no project .mcp.json":
            return tr("doctor.detail.mcp_none")
        if detail == "mcpServers must be a JSON object":
            return tr("doctor.detail.mcp_servers_invalid")
        match = re.match(r"(?P<count>\d+) server\(s\), workspace trusted$", detail)
        if match:
            return tr("doctor.detail.mcp_trusted", count=match.group("count"))
        match = re.match(r"(?P<count>\d+) server\(s\), run /mcp trust before launching project MCP servers$", detail)
        if match:
            return tr("doctor.detail.mcp_untrusted", count=match.group("count"))

    if check.name == "Release Gate":
        if detail == "scripts/check_release.py exists":
            return tr("doctor.detail.release_gate_ok")
        if detail == "scripts/check_release.py not found in this workspace":
            return tr("doctor.detail.release_gate_missing")

    return detail


def render_doctor_json(checks: Iterable[DiagnosticCheck]) -> str:
    """Render checks as stable machine-readable JSON."""
    check_list = list(checks)
    items = [
        {
            "name": check.name,
            "status": check.status,
            "detail": check.detail,
        }
        for check in check_list
    ]
    return json.dumps(
        {
            "ok": not has_failed_checks(check_list),
            "checks": items,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def build_support_bundle(
    workspace: str | Path | None = None,
    checks: Iterable[DiagnosticCheck] | None = None,
    *,
    audit_limit: int = 50,
) -> dict:
    """Build a redacted support payload without file contents or secrets."""
    workspace_path = Path(workspace).expanduser().resolve() if workspace else Path.cwd().resolve()
    check_list = list(checks) if checks is not None else run_doctor_checks(workspace_path)
    paths = SayacodePaths.resolve(create=False)
    return redact_value({
        "schema_version": 1,
        "ok": not has_failed_checks(check_list),
        "workspace": str(workspace_path),
        "platform": {
            "python": sys.version.split()[0],
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "paths": {
            "sayacode_home": str(paths.home),
            "user_config": str(paths.user_config),
            "api_configs": str(paths.api_configs),
            "sessions": str(paths.sessions_dir),
            "audit_log": str(paths.audit_log),
        },
        "checks": [
            {"name": check.name, "status": check.status, "detail": check.detail}
            for check in check_list
        ],
        "audit_events": read_recent_audit_events(limit=audit_limit),
    })


def write_support_bundle(
    target: str | Path,
    workspace: str | Path | None = None,
    checks: Iterable[DiagnosticCheck] | None = None,
) -> Path:
    """Write a redacted support bundle JSON file."""
    payload = build_support_bundle(workspace=workspace, checks=checks)
    path = Path(target).expanduser()
    if path.exists() and path.is_dir():
        path = path / "sayacode-support-bundle.json"
    if not path.suffix:
        path = path / "sayacode-support-bundle.json"
    from .private_io import write_private_json

    return write_private_json(path, payload)


def has_failed_checks(checks: Iterable[DiagnosticCheck]) -> bool:
    """Return True when any required check failed."""
    return any(check.status == "fail" for check in checks)


def _check_python() -> DiagnosticCheck:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info >= (3, 11):
        return DiagnosticCheck("Python", "ok", f"{version} satisfies >= 3.11")
    return DiagnosticCheck("Python", "fail", f"{version} is too old; Python >= 3.11 is required")


def _check_package_metadata() -> DiagnosticCheck:
    source_version = _read_source_version()
    try:
        installed_version = importlib.metadata.version("sayacode")
    except importlib.metadata.PackageNotFoundError:
        if source_version:
            return DiagnosticCheck("Package", "warn", f"sayacode {source_version} (source checkout)")
        return DiagnosticCheck("Package", "warn", "not installed as a package; running from source checkout")

    if source_version and source_version != installed_version:
        return DiagnosticCheck(
            "Package",
            "ok",
            f"sayacode {source_version} (source checkout; installed distribution {installed_version})",
        )
    return DiagnosticCheck("Package", "ok", f"sayacode {installed_version}")


def _read_source_version() -> str | None:
    init_path = Path(__file__).resolve().parents[1] / "__init__.py"
    try:
        content = init_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", content, re.MULTILINE)
    return match.group(1) if match else None


def _check_workspace(workspace: Path) -> DiagnosticCheck:
    if not workspace.exists():
        return DiagnosticCheck("Workspace", "fail", f"{workspace} does not exist")
    if not workspace.is_dir():
        return DiagnosticCheck("Workspace", "fail", f"{workspace} is not a directory")

    try:
        with tempfile.NamedTemporaryFile(
            dir=str(workspace),
            prefix=".sayacode-doctor-",
            delete=True,
        ):
            pass
    except OSError as exc:
        return DiagnosticCheck("Workspace", "fail", f"{workspace} is not writable: {exc}")

    return DiagnosticCheck("Workspace", "ok", f"{workspace} is writable")


def _check_git_available() -> DiagnosticCheck:
    git_path = shutil.which("git")
    if not git_path:
        return DiagnosticCheck("Git", "warn", "git executable was not found in PATH")

    try:
        result = subprocess.run(
            [git_path, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:
        return DiagnosticCheck("Git", "warn", f"git exists but could not run: {exc}")

    version = (result.stdout or result.stderr).strip()
    if result.returncode == 0:
        return DiagnosticCheck("Git", "ok", version or git_path)
    return DiagnosticCheck("Git", "warn", version or "git returned a non-zero exit code")


def _check_git_repository(workspace: Path) -> DiagnosticCheck:
    git_path = shutil.which("git")
    if not git_path or not workspace.exists() or not workspace.is_dir():
        return DiagnosticCheck("Git Repository", "warn", "skipped")

    result = subprocess.run(
        [git_path, "-C", str(workspace), "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode == 0 and result.stdout.strip() == "true":
        return DiagnosticCheck("Git Repository", "ok", f"{workspace} is inside a Git work tree")
    return DiagnosticCheck("Git Repository", "warn", "workspace is not a Git repository yet")


def _check_config_dir() -> DiagnosticCheck:
    path = SayacodePaths.resolve(create=False).home
    try:
        ensure_private_dir(path)
    except OSError as exc:
        return DiagnosticCheck("Config Directory", "fail", f"{path} is not writable: {exc}")
    return DiagnosticCheck("Config Directory", "ok", f"{path} is available")


def _check_config_schema() -> DiagnosticCheck:
    paths = SayacodePaths.resolve(create=False)
    legacy_files: list[str] = []
    expected = {
        paths.user_config: SAYACODE_CONFIG_SCHEMA_VERSION,
        paths.api_configs: API_CONFIG_SCHEMA_VERSION,
    }

    for path, version in expected.items():
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return DiagnosticCheck("Config Schema", "fail", f"{path} is invalid JSON: {exc}")
        if not isinstance(data, dict):
            return DiagnosticCheck("Config Schema", "fail", f"{path} must contain a JSON object")
        if data.get("schema_version") != version:
            legacy_files.append(str(path))

    if legacy_files:
        return DiagnosticCheck(
            "Config Schema",
            "warn",
            "legacy config detected; SAYACODE will not auto-migrate it. Reconfigure profiles with /model.",
        )

    return DiagnosticCheck("Config Schema", "ok", "current config schema")


def _check_session_schema(workspace: Path) -> DiagnosticCheck:
    state_dir = _workspace_session_dir(workspace)
    if not state_dir.exists():
        return DiagnosticCheck("Session Schema", "ok", "no saved sessions for this workspace")

    session_files = []
    mirror = state_dir / "session.json"
    if mirror.exists():
        session_files.append(mirror)
    sessions_dir = state_dir / "sessions"
    if sessions_dir.exists():
        session_files.extend(sessions_dir.glob("*/session.json"))

    legacy_files = []
    invalid_files = []
    for path in session_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            invalid_files.append(str(path))
            continue
        if not isinstance(data, dict) or data.get("schema_version") != SESSION_SCHEMA_VERSION:
            legacy_files.append(str(path))

    if invalid_files:
        return DiagnosticCheck("Session Schema", "fail", f"invalid session JSON: {invalid_files[0]}")
    if legacy_files:
        return DiagnosticCheck(
            "Session Schema",
            "warn",
            "legacy session data detected; SAYACODE will start a fresh session instead of auto-migrating it.",
        )

    return DiagnosticCheck("Session Schema", "ok", f"{len(session_files)} current session file(s)")


def _workspace_session_dir(workspace: Path) -> Path:
    return SayacodePaths.resolve(create=False).workspace_state_dir(workspace)


def _check_provider_environment() -> DiagnosticCheck:
    present = [name for name in PROVIDER_ENV_VARS if os.environ.get(name)]
    if present:
        return DiagnosticCheck("Provider Environment", "ok", ", ".join(present))
    return DiagnosticCheck(
        "Provider Environment",
        "warn",
        "no provider environment variables detected; interactive profile setup may be required",
    )


def _check_permission_policy(workspace: Path) -> DiagnosticCheck:
    paths = SayacodePaths.resolve(create=False)
    policy_files = [
        paths.user_permissions,
        paths.project_permissions(workspace),
    ]
    for path in policy_files:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return DiagnosticCheck("Permission Policy", "fail", f"{path} is invalid JSON: {exc}")
        if not isinstance(data, dict):
            return DiagnosticCheck("Permission Policy", "fail", f"{path} must contain a JSON object")

    policy = PermissionPolicy.load(workspace)
    return DiagnosticCheck(
        "Permission Policy",
        "ok",
        f"default={policy.default_action}, tools={len(policy.tool_rules)}",
    )


def _check_mcp_config(workspace: Path) -> DiagnosticCheck:
    config_path = workspace / ".mcp.json"
    if not config_path.exists():
        return DiagnosticCheck("MCP Config", "ok", "no project .mcp.json")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return DiagnosticCheck("MCP Config", "fail", f"{config_path} is invalid JSON: {exc}")
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    if not isinstance(servers, dict):
        return DiagnosticCheck("MCP Config", "fail", "mcpServers must be a JSON object")
    if is_mcp_workspace_trusted(workspace):
        return DiagnosticCheck("MCP Config", "ok", f"{len(servers)} server(s), workspace trusted")
    return DiagnosticCheck(
        "MCP Config",
        "warn",
        f"{len(servers)} server(s), run /mcp trust before launching project MCP servers",
    )


def _check_release_script(workspace: Path) -> DiagnosticCheck:
    script = workspace / "scripts" / "check_release.py"
    if script.exists():
        return DiagnosticCheck("Release Gate", "ok", "scripts/check_release.py exists")
    return DiagnosticCheck("Release Gate", "warn", "scripts/check_release.py not found in this workspace")


__all__ = [
    "build_support_bundle",
    "DiagnosticCheck",
    "has_failed_checks",
    "render_doctor_json",
    "render_doctor_report",
    "run_doctor_checks",
    "write_support_bundle",
]
