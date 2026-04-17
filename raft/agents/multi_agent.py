"""B6 多 Agent：2–3 角色（Planner、Execution、Verifier），每角色可配不同 LLM；共享 B5，每步将「最近 ExecutionResult + 当前状态」交给当前 Agent。"""
from typing import Any

from raft.contracts.models import AgentRole, ToolCall
from raft.agents.mock_agent import MockAgent


class MultiAgentRegistry:
    """
    多 Agent 注册表：按角色 (planner / execution / verifier) 分配 Agent；
    每角色可配置不同实现（如不同 LLM）；与 B4 结合：步骤 → 角色 → 本 Agent。
    """

    def __init__(
        self,
        *,
        planner: Any = None,
        execution: Any = None,
        verifier: Any = None,
    ) -> None:
        self._agents: dict[AgentRole, Any] = {
            "planner": planner or MockAgent(),
            "execution": execution or MockAgent(),
            "verifier": verifier or MockAgent(),
        }

    def get_agent(self, role: AgentRole) -> Any:
        """按角色返回对应 Agent 实例。"""
        return self._agents[role]

    def run(
        self,
        role: AgentRole,
        agent_input_context: dict,
        task_description: str = "",
    ) -> list[ToolCall]:
        """将本步交给指定角色的 Agent 执行，返回 tool_calls。"""
        agent = self.get_agent(role)
        return agent.run(
            agent_input_context=agent_input_context,
            task_description=task_description,
        )
