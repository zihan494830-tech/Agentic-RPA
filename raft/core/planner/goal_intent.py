"""GoalIntent：自然语言 goal 的结构化意图模型（五维）。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass
class GoalIntent:
    """将一句自然语言 goal 拆解为五个维度，供 planner 精确理解。

    五个维度：
    1. execution_constraints  硬约束  ── 明确指定了"用哪个 Agent"、"不超过 N 步"等不可违反的限制
    2. content_intent         内容意图 ── 用户期望产出的具体内容要点
    3. quality_requirements   质量要求 ── 对输出风格/长度/语言的期望
    4. scope_constraints      范围约束 ── 限定任务边界（市场、语言、时间范围等）
    5. temporal_relationships 时序关系 ── 明确的先后执行顺序
    """

    # ── 五个维度 ──────────────────────────────────────────────
    execution_constraints: list[str] = field(default_factory=list)
    """硬约束：必须严格遵守，LLM planner 不得违反。"""

    content_intent: list[str] = field(default_factory=list)
    """内容意图：输出需覆盖哪些要点。"""

    quality_requirements: list[str] = field(default_factory=list)
    """质量要求：简短/详细/中文/专业等。"""

    scope_constraints: list[str] = field(default_factory=list)
    """范围约束：仅限某市场/语言/时间段等。"""

    temporal_relationships: list[str] = field(default_factory=list)
    """时序关系：明确提到的步骤顺序。"""

    # ── 元信息 ────────────────────────────────────────────────
    ambiguities: list[str] = field(default_factory=list)
    """歧义项：解析时发现的模糊表达，附上歧义说明。"""

    confidence: float = 1.0
    """整体解析可信度 [0, 1]，有歧义或无法解析时降低。"""

    raw_goal: str = ""
    """原始 goal 文本，fallback 时使用。"""

    # ── 便捷方法 ──────────────────────────────────────────────

    def has_execution_constraints(self) -> bool:
        return bool(self.execution_constraints)

    def has_ambiguities(self) -> bool:
        return bool(self.ambiguities)

    def is_empty(self) -> bool:
        """是否所有维度均为空（解析无效时）。"""
        return not any([
            self.execution_constraints,
            self.content_intent,
            self.quality_requirements,
            self.scope_constraints,
            self.temporal_relationships,
        ])

    def to_planner_context(self) -> str:
        """生成供 planner system/user prompt 使用的结构化约束文本。

        硬约束优先放在最前、用醒目标题标注，确保 LLM 优先遵守。
        """
        parts: list[str] = []

        if self.execution_constraints:
            lines = "\n".join(f"  - {c}" for c in self.execution_constraints)
            parts.append(
                "【!! 硬约束 -- 必须严格遵守，绝对不可违反】\n"
                + lines
            )

        if self.content_intent:
            lines = "\n".join(f"  - {c}" for c in self.content_intent)
            parts.append("【内容意图 — 输出需覆盖以下要点】\n" + lines)

        if self.quality_requirements:
            lines = "\n".join(f"  - {c}" for c in self.quality_requirements)
            parts.append("【质量要求】\n" + lines)

        if self.scope_constraints:
            lines = "\n".join(f"  - {c}" for c in self.scope_constraints)
            parts.append("【范围约束】\n" + lines)

        if self.temporal_relationships:
            lines = "\n".join(f"  - {c}" for c in self.temporal_relationships)
            parts.append("【时序关系】\n" + lines)

        if self.ambiguities:
            lines = "\n".join(f"  - {a}" for a in self.ambiguities)
            parts.append(
                "【存在歧义 — 已按最保守方式理解，建议后续澄清】\n" + lines
            )

        return "\n\n".join(parts)


def goal_intent_from_dict(data: dict) -> GoalIntent:
    """从可序列化 dict 还原 GoalIntent（供 experiment.extra['planner_goal_intent'] 使用）。"""
    if not isinstance(data, dict):
        return GoalIntent()
    return GoalIntent(
        execution_constraints=list(data.get("execution_constraints") or []),
        content_intent=list(data.get("content_intent") or []),
        quality_requirements=list(data.get("quality_requirements") or []),
        scope_constraints=list(data.get("scope_constraints") or []),
        temporal_relationships=list(data.get("temporal_relationships") or []),
        ambiguities=list(data.get("ambiguities") or []),
        confidence=float(data.get("confidence", 1.0) or 1.0),
        raw_goal=str(data.get("raw_goal") or ""),
    )


def enrich_goal_intent_for_verification(intent: GoalIntent) -> GoalIntent:
    """当解析未产出 content_intent 时，用 raw_goal 作为单条要点，供规划 prompt 等使用。"""
    if intent.content_intent:
        return intent
    raw = (intent.raw_goal or "").strip()
    if raw:
        return replace(intent, content_intent=[raw])
    return intent
