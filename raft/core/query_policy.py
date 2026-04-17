"""
基于规则的下一轮 query 策略：根据上一轮（或历史）的得分、难度、错误类型决定「深化」或「换领域」及具体提示。
参考逻辑：高分且已掌握则换领域；高分未掌握则同领域加深；低分则同领域降难度/分解/换角度；中分则同领域类似变化。
换领域触发：放宽为「已完成至少 2 轮且上一轮得分 >= 0.8」即建议换领域，避免多轮都在同一领域细问。
"""
from typing import Any


def _infer_score(round_data: dict[str, Any]) -> float:
    """从单轮数据推断 0~1 综合得分。优先用 llm_judge 各维度均分，否则用 success/execution_success_rate。"""
    llm = round_data.get("llm_judge")
    if isinstance(llm, dict):
        vals = []
        for k in ("decision_quality", "output_quality", "tool_proficiency"):
            v = llm.get(k)
            if v is not None:
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    pass
        if vals:
            return round(sum(vals) / len(vals), 2)
    if round_data.get("success") is True:
        return 0.85
    rate = round_data.get("execution_success_rate")
    if rate is not None:
        try:
            return round(float(rate), 2)
        except (TypeError, ValueError):
            pass
    return 0.35


def _infer_difficulty(round_index: int) -> int:
    """当前轮次对应的难度档位 1~4（轮次越后难度越高，用于「是否已掌握」等判断）。"""
    return min(4, max(1, round_index))


def _infer_error_type(round_data: dict[str, Any]) -> str | None:
    """从单轮数据推断错误类型：UNDERSTANDING（理解/输出差）、PLANNING（重试/超时多）、否则 None。"""
    success = round_data.get("success")
    llm = round_data.get("llm_judge") or {}
    output_quality = llm.get("output_quality") if isinstance(llm, dict) else None
    retry = round_data.get("retry_count") or 0
    timeout = round_data.get("timeout_count") or 0
    try:
        retry = int(retry)
        timeout = int(timeout)
    except (TypeError, ValueError):
        retry = timeout = 0
    # 任务失败且输出质量低 → 理解为题/输出问题
    if success is False and output_quality is not None:
        try:
            if float(output_quality) < 0.5:
                return "UNDERSTANDING"
        except (TypeError, ValueError):
            pass
    if success is False and output_quality is None:
        return "UNDERSTANDING"
    # 重试或超时多 → 规划/执行问题
    if retry > 0 or timeout > 0:
        return "PLANNING"
    return None


def decide_next_strategy(previous_rounds: list[dict[str, Any]]) -> tuple[str, str]:
    """
    根据历史轮次（上一轮为主）决定下一轮策略与提示语。
    返回 (strategy, hint)：strategy 为 "deepen" 或 "diversify"，hint 为给 LLM 的具体出题要求（中文）。
    """
    if not previous_rounds:
        return ("diversify", "首轮从简单、易上手的新领域/话题开始，便于建立基线。")

    # 只依赖上一轮（小轮数测试版）
    prev = previous_rounds[-1]
    last_score = _infer_score(prev)
    # 上一轮对应的难度档位：当前已完成的轮数（1～4）
    difficulty = _infer_difficulty(len(previous_rounds))
    # 放宽换领域：已完成至少 2 轮且上一轮得分>=0.8 即建议换领域，避免多轮都在同一领域细问
    ready_to_diversify = (difficulty >= 2 and last_score >= 0.8) or (last_score >= 0.85 and difficulty >= 3)
    error_type = _infer_error_type(prev)

    if last_score >= 0.8:
        if ready_to_diversify:
            return ("diversify", "上一轮表现良好且已在该领域考察过，本轮请**换一个与之前完全不同的领域或话题**出题，考察 Agent 的多样化能力，避免继续在同一领域细问。")
        if difficulty < 4:
            return ("deepen", "上一轮表现良好，本轮在同一领域内提高难度、问得更细或更深入。")
        else:
            return ("deepen", "上一轮表现良好且难度已较高，本轮在同一领域内换不同技能点或维度考察。")
    elif last_score <= 0.5:
        if error_type == "UNDERSTANDING":
            return ("deepen", "上一轮理解或输出不佳，本轮在同一领域内降低难度、问得更简单明确。")
        elif error_type == "PLANNING":
            return ("deepen", "上一轮存在重试或超时，本轮在同一领域内分解步骤、用更细粒度的问题考察。")
        else:
            return ("deepen", "上一轮得分较低，本轮在同一领域内换一个角度或问法再测。")
    else:
        return ("deepen", "上一轮表现中等，本轮在同一领域内做类似但略有变化的 query，观察稳定性。")
