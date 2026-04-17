"""B2: 难度估计与单/多流路由；接口固定，可规则或可选 LLM 辅助编排决策。同时产出建议测试轮数（2～6）。"""
from typing import Callable

from raft.contracts.models import DifficultyRoutingResult, RouteType, TaskSpec

MIN_ROUNDS = 2
MAX_ROUNDS = 6


def _rounds_from_difficulty(difficulty: float, route_type: RouteType) -> int:
    """根据难度与路由类型映射建议轮数。"""
    diff = max(0.0, min(1.0, difficulty))
    if diff <= 0.3:
        base = 2
    elif diff <= 0.6:
        base = 3
    elif diff <= 0.8:
        base = 4
    else:
        base = 5
    if route_type == "multi_flow":
        base = min(MAX_ROUNDS, base + 1)
    return max(MIN_ROUNDS, min(MAX_ROUNDS, base))


def suggested_rounds_from_routing(routing: DifficultyRoutingResult) -> int:
    """从 B2 路由结果取建议轮数：若已含 suggested_rounds 则用其，否则按 difficulty/route_type 映射。"""
    if getattr(routing, "suggested_rounds", None) is not None:
        r = int(routing.suggested_rounds)
        return max(MIN_ROUNDS, min(MAX_ROUNDS, r))
    return _rounds_from_difficulty(routing.difficulty, routing.route_type)


def route(
    task_spec: TaskSpec,
    *,
    max_steps: int = 10,
    llm_router: Callable[[TaskSpec], DifficultyRoutingResult] | None = None,
) -> DifficultyRoutingResult:
    """
    根据 TaskSpec 决定 route_type：single_flow 或 multi_flow。
    - 若传入 llm_router（如 B2 的 LLMRouter），则用 LLM 辅助编排决策；
    - 否则规则实现：extra 中指定 route_type 则用其值；否则按描述关键词选 single_flow / multi_flow。
    """
    if llm_router is not None:
        return llm_router(task_spec)

    extra = task_spec.extra or {}
    if "route_type" in extra and extra["route_type"] in ("single_flow", "multi_flow"):
        route_type: RouteType = extra["route_type"]
    else:
        # 规则：描述较长或包含「多步」「分支」等倾向 multi_flow
        desc = (task_spec.description or "").lower()
        if any(kw in desc for kw in ("多步", "分支", "并行", "multi", "branch", "parallel")):
            route_type = "multi_flow"
        else:
            route_type = "single_flow"

    # 简单难度估计：按描述长度 0~1
    difficulty = min(1.0, len(task_spec.description or "") / 200.0)
    suggested_rounds = _rounds_from_difficulty(difficulty, route_type)
    # 注意：route_type 由规则/LLM 决策，不能被建议轮数反向覆盖。
    # 否则在 MIN_ROUNDS>=2 时会把所有任务强制成 multi_flow。

    return DifficultyRoutingResult(
        route_type=route_type,
        difficulty=difficulty,
        suggested_rounds=suggested_rounds,
        extra={"task_spec_id": task_spec.task_spec_id},
    )
