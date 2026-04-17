"""B6 Agent 抽象：接收 state + 最近 ExecutionResult，输出 tool_calls。"""
from typing import Protocol

from raft.contracts.models import ToolCall


class AgentProtocol(Protocol):
    """Agent 协议：agent_input_context + task_description → tool_calls。"""

    def run(
        self,
        agent_input_context: dict,
        task_description: str = "",
    ) -> list[ToolCall]:
        """根据当前状态与最近 RPA 结果输出本步要执行的 tool_calls。"""
        ...
