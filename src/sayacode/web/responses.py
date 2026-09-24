"""Web 响应的错误映射与字段白名单。"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any, TypeVar

from fastapi import HTTPException
from pydantic import BaseModel

from .security import redact_api

_T = TypeVar("_T")
_M = TypeVar("_M", bound=BaseModel)


async def call(operation: Awaitable[_T]) -> _T:
    """将可预期的产品错误映射到 HTTP；意外错误交给服务器记录。"""
    try:
        return await operation
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def view(model: type[_M], value: Mapping[str, Any]) -> _M:
    """先按模型丢弃私有字段，再对剩余字段逐层清除可识别凭据。"""
    projected = model.model_validate(value).model_dump()
    return model.model_validate(redact_api(projected))
