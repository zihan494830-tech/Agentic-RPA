"""DAG 感知调度器：替代 linearize_goal_plan + FIFO 队列，实现依赖驱动执行调度。"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raft.contracts.models import GoalPlan, GoalPlanStep

logger = logging.getLogger(__name__)


class StepState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING_HUMAN = "waiting_human"


class DAGScheduler:
    """
    DAG 感知执行调度器。

    使用方式：
        scheduler = DAGScheduler(plan)
        while not scheduler.is_done():
            ready = scheduler.next_ready()
            if not ready:
                break  # 没有可执行步骤（可能所有剩余步骤依赖失败步骤）
            step = ready[0]  # 单线程取第一个；并行场景可取全部
            scheduler.mark_running(step.step_id)
            result = execute(step)
            if result.success:
                scheduler.mark_done(step.step_id)
            else:
                scheduler.mark_failed(step.step_id)
    """

    def __init__(self, plan: "GoalPlan") -> None:
        from raft.contracts.models import GoalPlanStep

        self._steps: dict[str, "GoalPlanStep"] = {s.step_id: s for s in plan.steps}
        self._state: dict[str, StepState] = {
            sid: StepState.PENDING for sid in self._steps
        }
        # 失败步骤的下游（子图）会被标记为 SKIPPED
        self._failed: set[str] = set()
        # 预计算每个节点的后继（用于失败传播）
        self._children: dict[str, list[str]] = {sid: [] for sid in self._steps}
        for step in plan.steps:
            for dep in step.depends_on:
                if dep in self._children:
                    self._children[dep].append(step.step_id)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def next_ready(self) -> list["GoalPlanStep"]:
        """
        返回所有当前可执行的步骤（依赖全部完成且自身仍为 PENDING）。
        调用方可以串行取 [0] 执行，也可以并行提交全部。
        """
        ready: list["GoalPlanStep"] = []
        for sid, step in self._steps.items():
            if self._state[sid] != StepState.PENDING:
                continue
            deps_done = all(
                self._state.get(d) == StepState.DONE
                for d in step.depends_on
            )
            if deps_done:
                ready.append(step)
        # 按 step_id 排序保证确定性
        ready.sort(key=lambda s: s.step_id)
        return ready

    def is_done(self) -> bool:
        """所有步骤已进入终态（DONE / FAILED / SKIPPED / WAITING_HUMAN）。"""
        return all(
            s in (StepState.DONE, StepState.FAILED, StepState.SKIPPED, StepState.WAITING_HUMAN)
            for s in self._state.values()
        )

    def has_runnable(self) -> bool:
        """是否还有可执行步骤（PENDING 且依赖均 DONE）。"""
        return bool(self.next_ready())

    def pending_count(self) -> int:
        return sum(1 for s in self._state.values() if s == StepState.PENDING)

    def failed_count(self) -> int:
        return sum(1 for s in self._state.values() if s == StepState.FAILED)

    def get_state(self, step_id: str) -> StepState:
        return self._state.get(step_id, StepState.PENDING)

    def all_states(self) -> dict[str, str]:
        return {sid: s.value for sid, s in self._state.items()}

    # ------------------------------------------------------------------
    # 状态转换
    # ------------------------------------------------------------------

    def mark_running(self, step_id: str) -> None:
        if step_id in self._state:
            self._state[step_id] = StepState.RUNNING

    def mark_done(self, step_id: str) -> None:
        if step_id in self._state:
            self._state[step_id] = StepState.DONE
            logger.debug("[DAGScheduler] 步骤 %s 完成", step_id)

    def mark_failed(self, step_id: str, *, skip_downstream: bool = True) -> None:
        """
        标记步骤失败。
        skip_downstream=True 时递归跳过所有下游步骤（因为依赖已断）。
        """
        if step_id not in self._state:
            return
        self._state[step_id] = StepState.FAILED
        self._failed.add(step_id)
        logger.warning("[DAGScheduler] 步骤 %s 失败", step_id)
        if skip_downstream:
            self._skip_downstream(step_id)

    def mark_skipped(self, step_id: str) -> None:
        if step_id in self._state:
            self._state[step_id] = StepState.SKIPPED
            logger.debug("[DAGScheduler] 步骤 %s 已跳过（上游失败）", step_id)

    def mark_waiting_human(self, step_id: str) -> None:
        if step_id in self._state:
            self._state[step_id] = StepState.WAITING_HUMAN
            logger.warning("[DAGScheduler] 步骤 %s 等待人工确认", step_id)

    def reset_step(self, step_id: str) -> None:
        """将步骤重置为 PENDING（用于局部重试/replan 后插入新步骤）。"""
        if step_id in self._state:
            self._state[step_id] = StepState.PENDING
            self._failed.discard(step_id)

    # ------------------------------------------------------------------
    # 局部 replan 支持
    # ------------------------------------------------------------------

    def inject_steps(self, steps: list["GoalPlanStep"]) -> None:
        """
        将新步骤（通常来自 build_recovery_plan）注入调度器。
        新步骤以 PENDING 状态加入，原有失败步骤的 SKIPPED 下游可通过此接口替换。
        """
        for step in steps:
            self._steps[step.step_id] = step
            self._state[step.step_id] = StepState.PENDING
            self._failed.discard(step.step_id)
            # 更新后继关系
            if step.step_id not in self._children:
                self._children[step.step_id] = []
            for dep in step.depends_on:
                if dep in self._children:
                    if step.step_id not in self._children[dep]:
                        self._children[dep].append(step.step_id)
        logger.info("[DAGScheduler] 注入 %d 个恢复步骤", len(steps))

    def get_downstream(self, step_id: str) -> list[str]:
        """返回某步骤所有直接和间接下游的 step_id 列表。"""
        visited: set[str] = set()
        stack = [step_id]
        while stack:
            node = stack.pop()
            for child in self._children.get(node, []):
                if child not in visited:
                    visited.add(child)
                    stack.append(child)
        return list(visited)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _skip_downstream(self, step_id: str) -> None:
        for child in self._children.get(step_id, []):
            if self._state.get(child) == StepState.PENDING:
                self._state[child] = StepState.SKIPPED
                self._skip_downstream(child)
