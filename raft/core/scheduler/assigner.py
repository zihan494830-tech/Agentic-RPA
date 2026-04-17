"""B4: 步骤 → Agent 分配；工具 → RPA/API 路由；与 B3 结合为每步分配 Agent 与工具目标。"""
from raft.contracts.models import AgentRole, StepAssignment


def assign_step(step_index: int, *, dag_nodes: list[int] | None = None) -> StepAssignment:
    """
    步骤 → Agent 分配；工具目标默认 RPA。
    规则：按步序号轮询 planner → execution → verifier；可扩展为按 DAG 节点或配置表。
    """
    roles: list[AgentRole] = ["planner", "execution", "verifier"]
    role = roles[step_index % len(roles)]
    return StepAssignment(
        step_index=step_index,
        agent_role=role,
        tool_target="rpa",
        extra={"dag_nodes": dag_nodes} if dag_nodes is not None else {},
    )
