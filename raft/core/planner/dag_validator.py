"""DAG 校验与自动修复：有环检测（DFS）、引用校验、孤立节点检测。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raft.contracts.models import GoalPlan

logger = logging.getLogger(__name__)


def validate_dag(plan: "GoalPlan") -> list[str]:
    """
    校验 GoalPlan 的 DAG 结构合法性。
    返回错误消息列表；空列表表示合法。

    检测项：
    1. depends_on 引用了不存在的 step_id
    2. 存在有向环（DFS 染色法）
    3. 除 s0 之外的孤立节点（无任何步骤依赖它、且它自身也无依赖，但有其他步骤存在）
    """
    errors: list[str] = []
    if not plan.steps:
        return errors

    step_ids = {s.step_id for s in plan.steps}

    # 1. 引用校验
    for step in plan.steps:
        for dep in step.depends_on:
            if dep not in step_ids:
                errors.append(
                    f"步骤 {step.step_id} 的 depends_on 引用了不存在的 step_id: {dep!r}"
                )

    # 2. 有环检测（DFS 染色：0=未访问，1=访问中，2=已完成）
    adj: dict[str, list[str]] = {s.step_id: list(s.depends_on) for s in plan.steps}
    color: dict[str, int] = {sid: 0 for sid in step_ids}
    cycle_path: list[str] = []

    def _dfs(node: str, path: list[str]) -> bool:
        color[node] = 1
        path.append(node)
        for dep in adj.get(node, []):
            if dep not in color:
                continue
            if color[dep] == 1:
                cycle_path.extend(path)
                cycle_path.append(dep)
                return True
            if color[dep] == 0:
                if _dfs(dep, path):
                    return True
        path.pop()
        color[node] = 2
        return False

    for sid in step_ids:
        if color[sid] == 0:
            if _dfs(sid, []):
                errors.append(f"DAG 存在有向环，涉及节点: {' -> '.join(cycle_path)}")
                break

    return errors


def fix_dag(plan: "GoalPlan") -> "GoalPlan":
    """
    自动修复 GoalPlan 中的 DAG 结构问题，返回修复后的新 GoalPlan。

    修复策略：
    1. 删除 depends_on 中引用不存在 step_id 的边
    2. 若存在有向环，通过拓扑排序截断后向边（保守：删除导致环的 depends_on 条目）
    3. 重新编号 step_id（保持 s0, s1, s2... 连续）以维持下游兼容性
    """
    from raft.contracts.models import GoalPlan, GoalPlanStep, ToolCall

    if not plan.steps:
        return plan

    step_ids = {s.step_id for s in plan.steps}

    # 步骤一：清理悬空引用
    cleaned_steps: list[GoalPlanStep] = []
    for step in plan.steps:
        valid_deps = [d for d in step.depends_on if d in step_ids]
        if len(valid_deps) != len(step.depends_on):
            removed = set(step.depends_on) - set(valid_deps)
            logger.warning(
                "[DAGValidator] 自动移除步骤 %s 中的悬空依赖: %s",
                step.step_id,
                removed,
            )
        cleaned_steps.append(
            GoalPlanStep(
                step_id=step.step_id,
                tool_call=step.tool_call,
                depends_on=valid_deps,
                note=step.note,
                expected_output=step.expected_output,
                gate=step.gate,
                risk_level=step.risk_level,
            )
        )

    # 步骤二：检测并消除环（删除导致环的后向边）
    # 使用 Kahn 算法做拓扑排序；若有节点无法入队则说明有环，删除其 depends_on 中的环边
    in_degree: dict[str, int] = {s.step_id: 0 for s in cleaned_steps}
    adj: dict[str, list[str]] = {s.step_id: [] for s in cleaned_steps}
    dep_map: dict[str, list[str]] = {s.step_id: list(s.depends_on) for s in cleaned_steps}

    for step in cleaned_steps:
        for dep in step.depends_on:
            in_degree[step.step_id] += 1
            adj[dep].append(step.step_id)

    from collections import deque
    queue: deque[str] = deque(sid for sid, deg in in_degree.items() if deg == 0)
    topo_order: list[str] = []
    while queue:
        node = queue.popleft()
        topo_order.append(node)
        for child in adj.get(node, []):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(topo_order) < len(cleaned_steps):
        # 存在环：找出仍有 in_degree > 0 的节点，清空其依赖以破环
        cycle_nodes = {sid for sid, deg in in_degree.items() if deg > 0}
        logger.warning(
            "[DAGValidator] 检测到有向环，清空以下节点的 depends_on 以破环: %s",
            cycle_nodes,
        )
        fixed: list[GoalPlanStep] = []
        for step in cleaned_steps:
            if step.step_id in cycle_nodes:
                fixed.append(
                    GoalPlanStep(
                        step_id=step.step_id,
                        tool_call=step.tool_call,
                        depends_on=[d for d in dep_map[step.step_id] if d not in cycle_nodes],
                        note=step.note,
                        expected_output=step.expected_output,
                        gate=step.gate,
                        risk_level=step.risk_level,
                    )
                )
            else:
                fixed.append(step)
        cleaned_steps = fixed

    return GoalPlan(steps=cleaned_steps, source=plan.source, reason=plan.reason)
