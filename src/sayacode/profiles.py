"""模型画像的选择与配置持久化。Web 产品操作位于 host/products.py。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .config import Profile

if TYPE_CHECKING:
    from .application import SayacodeApp


def _new_profile_name(model_id: str, existing: dict[str, Profile]) -> str:
    """根据模型 ID 生成稳定且不冲突的本地配置名。"""
    base = re.sub(r"[^a-z0-9_-]+", "-", model_id.lower()).strip("-")[:48] or "model"
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"


def _profile(app: SayacodeApp) -> Profile:
    """单次模型覆盖优先，其次是应用已选配置和用户默认配置。"""
    if app.profile_override is not None:
        return app.profile_override
    if app.profile_name is not None:
        return app.config.profile(app.profile_name)
    return app.config.profile()


async def _save_config(app: SayacodeApp) -> None:
    await app.repository.save(app.config)


__all__ = ["_new_profile_name", "_profile", "_save_config"]
