"""记忆提取的最小材料和本机凭据过滤。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from langchain_core.messages import AnyMessage, ToolMessage

from .evidence import TurnEvidence

_ASSIGNMENT = re.compile(
    r"(?im)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|credential|authorization"
    r"|aws[_-]?(?:access[_-]?key[_-]?id|secret[_-]?access[_-]?key|session[_-]?token)"
    r"|client[_-]?secret|private[_-]?key|db[_-]?password)"
    r"\b(\s*[:=]\s*)['\"]?[^\s,'\"}]+"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}")
_URL_CREDENTIALS = re.compile(r"(?i)\b(https?://)[^\s/@:]+:[^\s/@]+@")
_SIGNED_QUERY = re.compile(
    r"(?i)([?&](?:x-amz-signature|x-amz-credential|api[_-]?key|access[_-]?token|token)=)[^&#\s]+"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)
_PRIVATE_KEY_HEADER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*")
_TOKEN = re.compile(
    r"\b(?:sk[-_][A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b"
)


def redact_secrets(text: str, configured_keys: Iterable[str] = ()) -> str:
    """移除已知凭据和常见密钥形态；过滤不构成沙箱或泄露保证。"""
    result = text
    for key in configured_keys:
        if key and len(key) >= 8:
            result = result.replace(key, "[已移除凭据]")
    result = _PRIVATE_KEY.sub("[已移除私钥]", result)
    result = _PRIVATE_KEY_HEADER.sub("[已移除私钥]", result)
    result = _BEARER.sub("Bearer [已移除凭据]", result)
    result = _TOKEN.sub("[已移除凭据]", result)
    result = _URL_CREDENTIALS.sub(r"\1[已移除凭据]@", result)
    result = _SIGNED_QUERY.sub(r"\1[已移除凭据]", result)
    return _ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[已移除凭据]", result)


def contains_secret(text: str, configured_keys: Iterable[str] = ()) -> bool:
    """拒绝将可识别的凭据写入长期记忆正文。"""
    return redact_secrets(text, configured_keys) != text


def _sensitive_read(message: ToolMessage, evidence: TurnEvidence) -> bool:
    if message.name != "read_file":
        return False
    call = evidence.tool_calls.get(str(message.tool_call_id))
    args = call.get("args", {}) if call else {}
    path = args.get("path") if isinstance(args, dict) else None
    if not isinstance(path, str):
        return False
    normalized = path.replace("\\", "/").casefold()
    name = Path(normalized).name
    return (
        name == ".env"
        or name.startswith(".env.")
        or name in {"id_rsa", "id_ed25519", "credentials.json", "secrets.toml"}
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
        or "/.ssh/" in normalized
    )


def extraction_evidence(
    evidence: TurnEvidence, configured_keys: Iterable[str] = ()
) -> TurnEvidence:
    """只把有限文本交给额外的整理模型，并保留原始来源 ID。"""
    selected: list[tuple[str, AnyMessage]] = []
    for source_id, message in zip(evidence.source_ids, evidence.messages, strict=True):
        if isinstance(message, ToolMessage) and _sensitive_read(message, evidence):
            continue
        if not isinstance(message.content, str):
            continue
        if isinstance(message, ToolMessage) and message.name == "execute_command_tool":
            # Shell 输出可能包含任意工作站凭据；整理模型只需要成败元数据。
            try:
                result = json.loads(message.content)
            except json.JSONDecodeError:
                continue
            if not isinstance(result, dict):
                continue
            body = json.dumps(
                {
                    key: result.get(key)
                    for key in ("exit_code", "timed_out", "stdout_bytes", "stderr_bytes")
                },
                ensure_ascii=False,
            )
            selected.append((source_id, message.model_copy(update={"content": body})))
            continue
        limit = 8192 if message.type == "human" else 4096
        body = redact_secrets(message.content, configured_keys)
        if len(body) > limit:
            body = f"{body[:limit]}\n[后续内容已省略]"
        selected.append((source_id, message.model_copy(update={"content": body})))
    allowed = {source_id for source_id, _ in selected}
    return TurnEvidence(
        messages=tuple(message for _, message in selected),
        source_ids=tuple(source_id for source_id, _ in selected),
        verified_source_ids=tuple(
            source_id for source_id in evidence.verified_source_ids if source_id in allowed
        ),
        tool_calls=evidence.tool_calls,
    )


__all__ = ["contains_secret", "extraction_evidence", "redact_secrets"]
