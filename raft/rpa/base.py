"""B7 RPA 适配器抽象：所有 RPA 实现返回统一 ExecutionResult。"""
from typing import Protocol

from raft.contracts.models import ExecutionResult, ToolCall


class RPAAdapter(Protocol):
    """RPA 适配器协议：tool_call + step_index → ExecutionResult。"""

    def execute(self, step_index: int, tool_call: ToolCall) -> ExecutionResult:
        """执行一次工具调用，返回统一 ExecutionResult（异常在内部捕获并归一化）。"""
        ...
