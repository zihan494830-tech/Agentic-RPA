"""B7 Mock: 可配置返回成功/失败/超时的 ExecutionResult。"""
from raft.contracts.models import ExecutionResult, ToolCall


class MockRPA:
    """Mock RPA：根据配置返回成功、失败或超时的 ExecutionResult。"""

    def __init__(
        self,
        *,
        fail_steps: set[int] | None = None,
        fail_step_ids: set[str] | None = None,
        timeout_steps: set[int] | None = None,
        timeout_step_ids: set[str] | None = None,
    ) -> None:
        """
        fail_steps: 在这些步数（0-based）返回失败 ExecutionResult（如 element_not_found）。
        fail_step_ids: 在这些计划 step_id 返回失败 ExecutionResult。
        timeout_steps: 在这些步数返回超时 ExecutionResult。
        timeout_step_ids: 在这些计划 step_id 返回超时 ExecutionResult。
        """
        self.fail_steps = fail_steps or set()
        self.fail_step_ids = fail_step_ids or set()
        self.timeout_steps = timeout_steps or set()
        self.timeout_step_ids = timeout_step_ids or set()

    def execute(self, step_index: int, tool_call: ToolCall) -> ExecutionResult:
        """执行一次工具调用，返回统一 ExecutionResult。"""
        step_id = getattr(tool_call, "step_id", None)
        if step_index in self.timeout_steps or (isinstance(step_id, str) and step_id in self.timeout_step_ids):
            return ExecutionResult(
                success=False,
                error_type="timeout",
                raw_response="Mock: operation timed out",
                output_text="",
                ui_state_delta=None,
                tool_name=tool_call.tool_name,
                step_id=step_id,
            )
        if step_index in self.fail_steps or (isinstance(step_id, str) and step_id in self.fail_step_ids):
            return ExecutionResult(
                success=False,
                error_type="element_not_found",
                raw_response="Mock: element not found",
                output_text="",
                ui_state_delta=None,
                tool_name=tool_call.tool_name,
                step_id=step_id,
            )
        return ExecutionResult(
            success=True,
            error_type=None,
            raw_response={"status": "ok"},
            output_text=f"Mock output for {step_id or f'step_{step_index}'}",
            ui_state_delta={"screen": f"step_{step_index}"},
            tool_name=tool_call.tool_name,
            step_id=step_id,
        )
