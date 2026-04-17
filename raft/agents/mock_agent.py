"""B6 Mock：接收 state + 最近 ExecutionResult，据此输出不同 tool_calls。MultiRoleMockAgent 按角色返回不同工具以体现多 Agent 编排。"""
from raft.contracts.models import AgentRole, ToolCall
from raft.core.state.models import SharedState


class MockAgent:
    """Mock Agent：根据「当前状态 + 最近 ExecutionResult」返回 tool_calls。"""

    def __init__(self, *, fail_after_step: int | None = None) -> None:
        """
        fail_after_step: 若为 1，则第 1 步后若上一步失败则第 2 步返回不同 tool（模拟根据失败做不同决策）。
        """
        self.fail_after_step = fail_after_step

    def run(
        self,
        agent_input_context: dict,
        task_description: str = "",
    ) -> list[ToolCall]:
        """
        输入：agent_input_context 含 current_step_index, state, last_execution_result（若有）。
        输出：本步要执行的 tool_calls。
        """
        step = agent_input_context.get("current_step_index", 0)
        last_result = agent_input_context.get("last_execution_result")

        # 第 1 步：固定发 open_system
        if step == 0:
            return [ToolCall(tool_name="open_system", params={"target": "demo"})]

        # 第 2 步及以后：若上一步失败则发 retry_operation，否则发 fetch_details
        if last_result and not last_result.get("success", True):
            return [ToolCall(tool_name="retry_operation", params={"reason": "last_failed"})]
        return [ToolCall(tool_name="fetch_details", params={"step": step})]


class MultiRoleMockAgent:
    """
    多角色 Mock Agent：按 planner / execution / verifier 返回不同 tool_calls，便于在报告中区分单 Agent 与多 Agent+DAG。
    - planner: plan_step / plan_next
    - execution: open_system / fetch_details（与 MockAgent 一致，体现执行层）
    - verifier: verify_step / verify_next
    """

    def __init__(self, *, role: AgentRole) -> None:
        self.role = role

    def run(
        self,
        agent_input_context: dict,
        task_description: str = "",
    ) -> list[ToolCall]:
        step = agent_input_context.get("current_step_index", 0)
        last_result = agent_input_context.get("last_execution_result")

        if self.role == "planner":
            tool = "plan_step" if step == 0 else "plan_next"
            return [ToolCall(tool_name=tool, params={"role": "planner", "step": step})]
        if self.role == "verifier":
            tool = "verify_step" if step == 0 else "verify_next"
            return [ToolCall(tool_name=tool, params={"role": "verifier", "step": step})]
        # execution 角色：与 MockAgent 行为一致
        if step == 0:
            return [ToolCall(tool_name="open_system", params={"target": "demo"})]
        if last_result and not last_result.get("success", True):
            return [ToolCall(tool_name="retry_operation", params={"reason": "last_failed"})]
        return [ToolCall(tool_name="fetch_details", params={"step": step})]


# 兼容旧名（历史文档/外部引用）
Phase2MockAgent = MultiRoleMockAgent
