"""节点级 Gate 验收：none / auto / human 三级审核逻辑。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raft.contracts.models import GoalPlanStep

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    """Gate 验收结果。"""
    passed: bool
    """验收是否通过。"""
    action: str
    """后续动作：'continue'（继续执行）/ 'replan'（触发重规划）/ 'wait_human'（等待人工）/ 'skip'（跳过本步）。"""
    reason: str = ""
    """验收结论说明。"""
    details: dict[str, Any] = field(default_factory=dict)
    """附加细节（可用于日志/审计）。"""


def check_gate(
    step: "GoalPlanStep",
    execution_result: Any = None,
    *,
    human_confirm_fn: Any = None,
) -> GateResult:
    """
    对已执行完毕的步骤（或即将执行的写操作）做 gate 验收。

    参数：
        step             : 当前 GoalPlanStep
        execution_result : 执行结果（ExecutionResult 或 None）
        human_confirm_fn : 可选回调，签名为 (step, execution_result) -> bool；
                           用于 gate="human" 时的人工确认接口。

    返回 GateResult：
        passed=True  + action='continue'   → 通过，继续下一步
        passed=False + action='replan'     → 不通过，触发局部重规划
        passed=False + action='wait_human' → 需人工介入（gate='human' 且无回调时）
        passed=False + action='skip'       → 可忽略的非关键失败
    """
    gate = getattr(step, "gate", "none")
    risk_level = getattr(step, "risk_level", "low")
    expected_output = getattr(step, "expected_output", None)

    # ----------------------------------------------------------------
    # gate = "none"：直接通过
    # ----------------------------------------------------------------
    if gate == "none":
        return GateResult(
            passed=True,
            action="continue",
            reason="gate=none，无需验收",
        )

    # ----------------------------------------------------------------
    # gate = "human"：人工确认
    # ----------------------------------------------------------------
    if gate == "human":
        logger.info(
            "[Gate] 步骤 %s (risk=%s) 需要人工确认: %s",
            step.step_id,
            risk_level,
            step.tool_call.tool_name if step.tool_call else "?",
        )
        if human_confirm_fn is not None:
            try:
                confirmed = human_confirm_fn(step, execution_result)
            except Exception as exc:
                logger.warning("[Gate] human_confirm_fn 调用失败: %s", exc)
                confirmed = False
            if confirmed:
                return GateResult(
                    passed=True,
                    action="continue",
                    reason="人工已确认",
                )
            return GateResult(
                passed=False,
                action="replan",
                reason="人工拒绝，触发重规划",
            )
        # 没有回调：返回 wait_human，让调用方决定如何处理
        return GateResult(
            passed=False,
            action="wait_human",
            reason=f"步骤 {step.step_id} 标记为 human gate，等待人工确认",
            details={
                "tool_name": step.tool_call.tool_name if step.tool_call else "",
                "risk_level": risk_level,
                "expected_output": expected_output,
            },
        )

    # ----------------------------------------------------------------
    # gate = "auto"：规则自动校验
    # ----------------------------------------------------------------
    if gate == "auto":
        return _auto_check(step, execution_result)

    # 未知 gate 值，保守放行
    logger.warning("[Gate] 未知 gate 值 %r，保守放行步骤 %s", gate, step.step_id)
    return GateResult(passed=True, action="continue", reason=f"未知 gate={gate!r}，放行")


# ------------------------------------------------------------------
# 自动验收规则
# ------------------------------------------------------------------

def _auto_check(step: "GoalPlanStep", execution_result: Any) -> GateResult:
    """
    auto gate 的规则校验：
    1. execution_result 不为 None
    2. execution_result.success 为 True（如果有该字段）
    3. 若 step.expected_output 不为空，且 execution_result 有 output 字段，
       则检查 output 非空（语义校验留给更高层；这里只做存在性断言）
    """
    if execution_result is None:
        return GateResult(
            passed=False,
            action="replan",
            reason="auto gate：执行结果为 None，触发重规划",
        )

    # 检查 .success
    success = getattr(execution_result, "success", None)
    if success is False:
        error_msg = getattr(execution_result, "error", "") or ""
        return GateResult(
            passed=False,
            action="replan",
            reason=f"auto gate：执行结果 success=False，error={error_msg!r}",
            details={"error": error_msg},
        )

    # 检查输出非空（如果 expected_output 不为空）
    expected_output = getattr(step, "expected_output", None)
    if expected_output:
        output = _extract_text_output(execution_result)
        if not output:
            return GateResult(
                passed=False,
                action="replan",
                reason="auto gate：expected_output 要求有内容，但未提取到稳定输出",
            )

    return GateResult(
        passed=True,
        action="continue",
        reason="auto gate：规则校验通过",
    )


def _extract_text_output(execution_result: Any) -> str:
    """从统一 ExecutionResult 中提取最稳定的文本产出。"""
    if execution_result is None:
        return ""

    direct = getattr(execution_result, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    raw_response = getattr(execution_result, "raw_response", None)
    if isinstance(raw_response, str) and raw_response.strip():
        return raw_response.strip()
    if isinstance(raw_response, dict):
        for key in ("response", "final_report", "text", "output", "message"):
            value = raw_response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    ui_state_delta = getattr(execution_result, "ui_state_delta", None)
    if isinstance(ui_state_delta, dict):
        for key in ("response_text", "poffices_response", "final_report", "text"):
            value = ui_state_delta.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    data = getattr(execution_result, "data", None)
    if isinstance(data, str) and data.strip():
        return data.strip()
    if isinstance(data, dict):
        for key in ("response", "text", "output"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return ""
