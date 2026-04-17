"""B8 评估：基础指标（成功/失败、步骤数）；扩展指标（RPA/鲁棒性、可选 LLM-as-judge）。"""
import json
from pathlib import Path
from typing import Any

from raft.contracts.models import RunMetrics, RuleCriteriaConfig, TaskSpec, TrajectoryEntry


# 轨迹落盘格式版本，便于批量重放与解析
TRAJECTORY_SCHEMA_VERSION = "1.0"


def _check_success_simple(trajectory: list[TrajectoryEntry], task_spec: TaskSpec) -> bool:
    """
    简单成功判定：最后一步所有 execution 成功；若存在 ground_truth，则关键字段需在 state 或 ui_state_delta 中出现。
    """
    if not trajectory:
        return False
    last_entry = trajectory[-1]
    last_step = last_entry.step_result
    all_ok = all(er.success for er in last_step.execution_results)
    if not all_ok:
        return False
    gt = task_spec.ground_truth
    if gt is None or not gt:
        return True
    # 聚合：agent_input 的 state + 本步各 execution 的 ui_state_delta
    state_snapshot = last_step.agent_input_snapshot or {}
    aggregated: dict[str, Any] = dict(state_snapshot.get("state") or {})
    for er in last_step.execution_results:
        if er.ui_state_delta:
            aggregated.update(er.ui_state_delta)
    for key, expected in gt.items():
        if aggregated.get(key) != expected:
            return False
    return True


def _compute_extended_rpa_metrics(entries: list[TrajectoryEntry]) -> dict[str, Any]:
    """从轨迹计算 RPA/鲁棒性扩展指标（execution 成功率、重试、超时、恢复等）。"""
    total_executions = 0
    success_executions = 0
    retry_count = 0
    timeout_count = 0
    failed_step_indices: set[int] = set()
    recovered_after_fail: set[int] = set()
    failed_tool_names: list[str] = []
    seen_error_types: list[str] = []

    for i, entry in enumerate(entries):
        sr = entry.step_result
        for tc in sr.tool_calls:
            if tc.tool_name == "retry_operation":
                retry_count += 1
        for j, er in enumerate(sr.execution_results):
            total_executions += 1
            if er.success:
                success_executions += 1
            else:
                if er.error_type == "timeout":
                    timeout_count += 1
                failed_step_indices.add(i)
                # collect tool name and error type for this failure
                if j < len(sr.tool_calls):
                    tn = sr.tool_calls[j].tool_name
                    if tn and tn not in failed_tool_names:
                        failed_tool_names.append(tn)
                if er.error_type and er.error_type not in seen_error_types:
                    seen_error_types.append(er.error_type)
        # 上一步失败且当前步有成功 execution → 记为一次恢复
        if i > 0 and (i - 1) in failed_step_indices and any(er.success for er in sr.execution_results):
            recovered_after_fail.add(i - 1)

    execution_success_rate = (success_executions / total_executions) if total_executions else 0.0
    step_count = len(entries)
    timeout_rate = (timeout_count / step_count) if step_count else 0.0
    failure_count = len(failed_step_indices)
    recovery_rate = (len(recovered_after_fail) / failure_count) if failure_count else None

    return {
        "execution_success_rate": round(execution_success_rate, 4),
        "retry_count": retry_count,
        "timeout_count": timeout_count,
        "timeout_rate": round(timeout_rate, 4),
        "recovery_count": len(recovered_after_fail),
        "recovery_rate": round(recovery_rate, 4) if recovery_rate is not None else None,
        "failed_tools": sorted(set(failed_tool_names)),
        "error_types": sorted(set(seen_error_types)),
    }


def evaluate_rule_criteria(
    trajectory: list[dict],
    task_spec: TaskSpec,
) -> dict[str, Any]:
    """
    规则型判据：检查轨迹是否满足 required_tool_calls、required_step_success 等。
    从 task_spec.extra.rule_criteria 读取配置；返回 { "passed": bool, "details": {...} }。
    """
    extra = getattr(task_spec, "extra", None) or {}
    rc = extra.get("rule_criteria")
    if not rc:
        return {"passed": True, "details": {}, "required_tools_passed": True, "required_step_success_passed": True}
    if isinstance(rc, dict):
        cfg = RuleCriteriaConfig.model_validate(rc)
    else:
        cfg = rc
    entries = [TrajectoryEntry.model_validate(e) for e in trajectory]
    all_tool_names: set[str] = set()
    step_success: dict[int, bool] = {}
    for e in entries:
        sr = e.step_result
        for tc in sr.tool_calls:
            all_tool_names.add(tc.tool_name)
        step_success[e.step_index] = all(er.success for er in sr.execution_results) if sr.execution_results else False
    required_tools_passed = all(t in all_tool_names for t in cfg.required_tool_calls)
    required_step_success_passed = all(step_success.get(i, False) for i in cfg.required_step_success)
    passed = required_tools_passed and required_step_success_passed
    details = {
        "required_tool_calls": cfg.required_tool_calls,
        "actual_tool_calls": sorted(all_tool_names),
        "required_tools_passed": required_tools_passed,
        "required_step_success": cfg.required_step_success,
        "required_step_success_passed": required_step_success_passed,
    }
    return {"passed": passed, "details": details, "required_tools_passed": required_tools_passed, "required_step_success_passed": required_step_success_passed}


def evaluate_trajectory(
    trajectory: list[dict],
    task_spec: TaskSpec,
    run_id: str | None = None,
    *,
    extended: bool = True,
    use_llm_judge: bool = False,
    llm_judge_provider: str | None = None,
) -> RunMetrics:
    """
    B8 评估：根据轨迹与 TaskSpec 计算成功与否、步骤数；extended=True 时计算 RPA/鲁棒性扩展指标。
    use_llm_judge=True 时接入 LLM-as-judge 对轨迹评分（决策质量、推理连贯性、工具熟练度），结果写入 RunMetrics.llm_judge。
    trajectory 为已序列化的 list[dict]（如 serialize_trajectory 产出）。
    """
    entries = [TrajectoryEntry.model_validate(e) for e in trajectory]
    step_count = len(entries)
    success = _check_success_simple(entries, task_spec)
    details: dict[str, Any] = {}
    execution_success_rate = None
    retry_count = None
    timeout_count = None
    timeout_rate = None
    recovery_count = None
    recovery_rate = None
    failed_tools: list[str] = []
    error_types: list[str] = []
    llm_judge = None
    rule_criteria = None

    if extended:
        ext = _compute_extended_rpa_metrics(entries)
        execution_success_rate = ext["execution_success_rate"]
        retry_count = ext["retry_count"]
        timeout_count = ext["timeout_count"]
        timeout_rate = ext["timeout_rate"]
        recovery_count = ext["recovery_count"]
        recovery_rate = ext["recovery_rate"]
        failed_tools = ext["failed_tools"]
        error_types = ext["error_types"]
        details.update(ext)

    rule_criteria = evaluate_rule_criteria(trajectory, task_spec)
    if rule_criteria:
        details["rule_criteria"] = rule_criteria

    if use_llm_judge:
        llm_judge = llm_judge_trajectory(
            trajectory, task_spec, provider=llm_judge_provider
        )

    return RunMetrics(
        success=success,
        step_count=step_count,
        run_id=run_id,
        details=details,
        execution_success_rate=execution_success_rate,
        retry_count=retry_count,
        timeout_count=timeout_count,
        timeout_rate=timeout_rate,
        recovery_count=recovery_count,
        recovery_rate=recovery_rate,
        failed_tools=failed_tools,
        error_types=error_types,
        llm_judge=llm_judge,
        rule_criteria=rule_criteria,
    )


def llm_judge_trajectory(
    trajectory: list[dict],
    task_spec: TaskSpec,
    *,
    provider: str | None = None,
) -> dict[str, Any] | None:
    """
    LLM-as-judge 对轨迹评分（决策质量、推理连贯性、工具熟练度）。
    支持 provider：openai、qwen、grok（与 B2/query_suggester 一致）；未安装 openai 或未配置 API Key 时返回 None。
    """
    try:
        from raft.core.llm_judge import judge_trajectory
        return judge_trajectory(trajectory, task_spec, provider=provider)
    except Exception:
        return None


def write_trajectory_log(
    trajectory: list[dict],
    task_spec: TaskSpec,
    run_id: str,
    log_dir: Path,
    *,
    experiment_id: str = "",
    extra: dict[str, Any] | None = None,
    schema_version: str = TRAJECTORY_SCHEMA_VERSION,
) -> Path:
    """
    轨迹日志落盘：将 trajectory（含 prompts、tool_calls、ExecutionResults、状态快照）写入 log_dir。
    文件名：{run_id}.json 或 {experiment_id}_{run_id}.json。
    schema_version 便于批量重放与解析。
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    name = f"{experiment_id}_{run_id}.json" if experiment_id else f"{run_id}.json"
    path = log_dir / name
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "run_id": run_id,
        "experiment_id": experiment_id,
        "task_spec_id": task_spec.task_spec_id,
        "task_spec": task_spec.model_dump(),
        "trajectory": trajectory,
        "step_count": len(trajectory),
    }
    if extra:
        payload["extra"] = extra
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
