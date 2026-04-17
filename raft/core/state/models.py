"""B5: 共享状态与轨迹条目（与契约中的 TrajectoryEntry/StepResult 一致）。"""
from raft.contracts.models import ExecutionResult, StepResult, TrajectoryEntry
from pydantic import BaseModel, Field
from typing import Any


class SharedState(BaseModel):
    """共享状态：含最近一次 ExecutionResult，供闭环 1 下一轮 Agent 输入。"""
    current_step_index: int = Field(default=0, description="当前步序号")
    last_execution_result: ExecutionResult | None = Field(default=None, description="最近一次 RPA 执行结果")
    state: dict[str, Any] = Field(default_factory=dict, description="通用状态字典")
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展字段")


__all__ = ["SharedState", "StepResult", "TrajectoryEntry", "ExecutionResult"]
