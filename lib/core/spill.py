"""工具大输出落盘：给模型预览 + 定位符，完整内容留在磁盘。

截断到上限会把尾部直接丢掉，之后无法找回；落盘则让模型可以按定位符再读。
（Shell 工具早已这么做，这里补上其它工具共用的实现。）
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .private_io import ensure_private_dir

logger = logging.getLogger(__name__)

# 与 Shell 工具共用同一个输出根目录：落盘必须在**工作区内**，因为 read_file
# 经 sanitize_path 强制工作区限定——落在工作区外的定位符模型读不到。
OUTPUT_DIR_NAME = ".sayacode_outputs"
SPILL_DIR_NAME = "spill"


def spill_root(workspace: Any) -> Path:
    """工作区级 spill 根目录。"""
    root = Path(workspace).expanduser().resolve()
    return root / OUTPUT_DIR_NAME / SPILL_DIR_NAME


def _safe_stem(value: str) -> str:
    """把来源名压成单个安全路径段，防路径穿越。"""
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return stem.strip("._-")[:40] or "output"


def spill_text(
    workspace: Any,
    source: str,
    text: str,
    suggested_name: str = "",
) -> Optional[Path]:
    """把完整文本写入 spill 目录并返回路径；失败返回 None。

    返回 None 时调用方保留原有的截断行为——落盘是增强，不是新故障点。
    """
    try:
        root = spill_root(workspace)
        ensure_private_dir(root)
        # 取 8 位内容哈希防同名覆盖，时间戳到秒区分并发落盘。
        digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        stem = _safe_stem(suggested_name or source)
        path = root / f"{stem}-{stamp}-{digest}.txt"
        path.write_text(text, encoding="utf-8")
        return path
    except Exception as exc:
        logger.debug("工具输出落盘失败，回退截断: %s", exc)
        return None


def preview_with_locator(text: str, path: Path, limit: int) -> str:
    """构造面向模型的预览：前 limit 字符 + 完整内容定位符。"""
    omitted = max(0, len(text) - limit)
    return f"{text[:limit]}\n... [{omitted} 字符未显示，完整内容见: {path}]"


__all__ = ["SPILL_DIR_NAME", "preview_with_locator", "spill_root", "spill_text"]
