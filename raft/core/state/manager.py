"""B5: State & Trajectory Manager — 内存版，按回合 append；状态含「最近一次 ExecutionResult」。"""
from raft.contracts.models import ExecutionResult, StepResult, ToolCall, TrajectoryEntry
from raft.core.state.models import SharedState


class StateAndTrajectoryManager:
    """内存版状态与轨迹管理：写入状态与轨迹，供下一轮注入 Agent。"""

    def __init__(self) -> None:
        self._state = SharedState()
        self._trajectory: list[TrajectoryEntry] = []

    @property
    def state(self) -> SharedState:
        return self._state

    @property
    def trajectory(self) -> list[TrajectoryEntry]:
        return list(self._trajectory)

    def append_trajectory_entry(self, entry: TrajectoryEntry) -> None:
        self._trajectory.append(entry)

    def update_state(
        self,
        *,
        current_step_index: int | None = None,
        last_execution_result: ExecutionResult | None = None,
        state_delta: dict | None = None,
    ) -> None:
        if current_step_index is not None:
            self._state.current_step_index = current_step_index
        if last_execution_result is not None:
            self._state.last_execution_result = last_execution_result
        if state_delta is not None:
            self._state.state = {**self._state.state, **state_delta}

    def record_step(
        self,
        step_index: int,
        tool_calls: list[ToolCall],
        execution_results: list[ExecutionResult],
        agent_input_snapshot: dict | None = None,
    ) -> None:
        """记录一步并写入轨迹；同步更新 B5 状态：
        - last_execution_result：优先取最后一个成功的 execution，全部失败时取最后一个。
        - state（ui_state_delta 合并）：按执行顺序顺序覆盖合并所有 execution 的 delta（与 B8 评估逻辑一致）。
          同一 key 后者覆盖前者；各 execution 应使用不相交的 key 以避免歧义（见 contracts/models.py 约定）。
        """
        last_result: ExecutionResult | None = None
        if execution_results:
            # 优先找最后一个成功的 execution，失败时至少保留最后一个
            successful = [er for er in execution_results if er and er.success]
            last_result = successful[-1] if successful else execution_results[-1]
        self.update_state(
            current_step_index=step_index,
            last_execution_result=last_result,
        )
        for er in execution_results:
            if er and getattr(er, "ui_state_delta", None):
                self.update_state(state_delta=er.ui_state_delta)
        step_result = StepResult(
            step_index=step_index,
            tool_calls=tool_calls,
            execution_results=execution_results,
            agent_input_snapshot=agent_input_snapshot,
        )
        self.append_trajectory_entry(
            TrajectoryEntry(step_index=step_index, step_result=step_result)
        )

    def get_agent_input_context(self) -> dict:
        """供下一轮 Agent 输入：当前状态 + 最近 ExecutionResult。"""
        out: dict = {
            "current_step_index": self._state.current_step_index,
            "state": dict(self._state.state),
        }
        if self._state.last_execution_result is not None:
            out["last_execution_result"] = self._state.last_execution_result.model_dump()
        return out

    def serialize_trajectory(self) -> list[dict]:
        """轨迹可序列化并写回。"""
        return [e.model_dump() for e in self._trajectory]
