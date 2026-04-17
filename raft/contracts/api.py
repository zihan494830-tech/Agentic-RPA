"""统一 Block API 请求/响应信封与错误表示（见 API_CONTRACT.md）。"""
from typing import Any

from pydantic import BaseModel, Field


class BlockRequest(BaseModel):
    """Block 入参统一包装。"""
    request_id: str | None = Field(default=None, description="调用方请求唯一标识")
    block_id: str | None = Field(default=None, description="被调 Block 标识，如 B1, B6")
    api_version: str = Field(default="v1", description="契约版本")
    payload: dict[str, Any] = Field(..., description="Block 业务入参")
    options: dict[str, Any] = Field(default_factory=dict, description="可选参数")


class ApiError(BaseModel):
    """统一错误表示。"""
    code: str = Field(..., description="机器可读错误码")
    message: str = Field(..., description="人类可读说明")
    details: dict[str, Any] | None = Field(default=None, description="额外上下文")


class BlockResponse(BaseModel):
    """Block 出参统一包装。"""
    request_id: str | None = Field(default=None, description="与请求一致")
    block_id: str | None = Field(default=None, description="与请求一致")
    code: str | int = Field(..., description="业务结果码，成功建议 0 或 'ok'")
    data: dict[str, Any] | None = Field(default=None, description="成功时业务出参")
    error: ApiError | None = Field(default=None, description="失败时错误结构")
