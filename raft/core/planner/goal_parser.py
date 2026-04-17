"""GoalParser：使用 LLM 将自然语言 goal 解析为结构化 GoalIntent。

职责：
  1. 调用 LLM，将 goal 拆成五个维度（硬约束 / 内容意图 / 质量要求 / 范围约束 / 时序关系）
  2. 标记歧义项（ambiguities）
  3. 返回 GoalIntent；任何异常均 fallback 为 GoalIntent(raw_goal=goal)

设计原则：
  - 本模块是 *纯解析*，不做规划；输出只描述意图，不决定执行方案。
  - 解析失败不会抛出异常，保证对 planner 零侵入。
"""
from __future__ import annotations

import json
import logging
import os
import re

from raft.core.planner.goal_intent import GoalIntent, enrich_goal_intent_for_verification

try:
    from openai import OpenAI  # noqa: F401  (仅用于存在性检测)
except ImportError:
    OpenAI = None  # type: ignore

from raft.core.llm_client import chat_completion_with_retry
from raft.core.llm_providers import normalize_provider, resolve_api_key, resolve_base_url, resolve_chat_model

logger = logging.getLogger(__name__)

# ── System Prompt ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
你是目标语义分析器。将用户自然语言的任务 goal 解析为结构化 JSON，输出格式如下（必须是合法 JSON，不得输出其他内容）：

{
  "execution_constraints": ["只使用 Agent: X", "最多调用 2 步"],
  "content_intent": ["给出3个竞品", "分析优劣势", "给出进入建议"],
  "quality_requirements": ["简短", "中文输出"],
  "scope_constraints": ["仅限国内市场"],
  "temporal_relationships": ["先完成搜索再汇总"],
  "ambiguities": ["'竞争分析'语义不明：可能指用户期望的输出内容，也可能指 Agent 名称"],
  "confidence": 0.9
}

【解析规则】

execution_constraints（硬约束）:
  - 用户明确指定了"用哪个/哪些 Agent"、"不超过N步"、"只能用某工具"等 **不可违反** 的限制
  - 格式："只使用 Agent: <名称>" 或 "不得使用: <名称>" 等
  - 例："用 Market Analysis 帮我做" → ["只使用 Agent: Market Analysis"]
  - 若 goal 中无明确限制 → []

content_intent:
  - 用户期望输出包含的具体内容要点（与执行方式无关，只描述"要什么"）
  - 例："3个竞品"、"每个竞品的优劣势"、"市场进入建议"

quality_requirements:
  - 对输出风格、长度、语言的期望，如"简短"、"详细"、"中文"、"专业报告格式"

scope_constraints:
  - 限定任务边界，如"仅限国内市场"、"只看近3个月数据"、"聚焦移动端"

temporal_relationships:
  - 用户明确提到的步骤顺序，如"先搜索再汇总"、"最后生成报告"
  - 若 goal 未提及顺序 → []

ambiguities:
  - 语义不确定的词或短语，说明具体歧义点
  - 宁可多标记，不要漏标

confidence:
  - 整体解析可信度 [0,1]；歧义越多、约束越模糊，数值越低

注意：所有字段均为必填，空时填 []，不要省略任何字段。只输出 JSON。\
"""


# ── Execution constraints sanitization ─────────────────────────────────────

def _goal_has_explicit_agent_hard_constraints(raw_goal: str) -> bool:
    """仅当 goal 中明确出现“只使用/不得使用”等表述时，才保留 LLM 输出的 Agent 相关硬约束。"""
    if not raw_goal:
        return False
    return bool(
        re.search(
            r"(只使用|仅使用|不得使用|不要使用|禁止使用|only use\s*Agent|do not use\s*Agent)",
            raw_goal,
            flags=re.I,
        )
    )


def _goal_has_explicit_max_steps_hard_constraint(raw_goal: str) -> bool:
    """仅当 goal 中明确出现“最多/不超过 … 步”时，才保留 LLM 输出的步数硬约束。"""
    if not raw_goal:
        return False
    return bool(
        re.search(
            r"(最多调用|最多|不超过|不大于)\s*\d+\s*步",
            raw_goal,
            flags=re.I,
        )
    )


def _sanitize_execution_constraints(intent: GoalIntent, raw_goal: str) -> GoalIntent:
    """
    对 LLM 输出的 execution_constraints 做净化：
    - 如果 goal 中未明确给出“只使用/不得使用”等约束，则丢弃对应硬约束；
    - 如果 goal 中未明确给出“最多/不超过 … 步”，则丢弃步数硬约束。

    这样避免示例模板污染（LLM 将示例约束“默认填入”）导致误生成不存在的硬约束。
    """
    if not intent.execution_constraints:
        return intent

    keep_agent_constraints = _goal_has_explicit_agent_hard_constraints(raw_goal)
    keep_max_steps = _goal_has_explicit_max_steps_hard_constraint(raw_goal)

    sanitized: list[str] = []
    for c in intent.execution_constraints:
        if re.search(
            r"(只使用\s*Agent:|only use\s*Agent:|不得使用:|do not use\s*Agent:)",
            c,
            flags=re.I,
        ):
            if keep_agent_constraints:
                sanitized.append(c)
            continue
        if re.search(r"(最多调用|不超过|不大于|最多)\s*\d+\s*步", c, flags=re.I):
            if keep_max_steps:
                sanitized.append(c)
            continue

        # 其它未知约束：尽量保留
        sanitized.append(c)

    if sanitized == intent.execution_constraints:
        return intent

    intent.execution_constraints = sanitized
    return intent


# ── Public API ───────────────────────────────────────────────────────────────

def parse_goal(
    goal: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> GoalIntent:
    """将自然语言 goal 解析为 GoalIntent。

    Args:
        goal:     原始目标字符串。
        provider: LLM provider（"qwen" 等），None 则用默认。
        model:    模型名，None 则按 provider 选默认。

    Returns:
        GoalIntent；解析失败时返回 GoalIntent(raw_goal=goal, confidence=0)。
    """
    if not goal or not goal.strip():
        return enrich_goal_intent_for_verification(GoalIntent(raw_goal=goal or ""))

    if OpenAI is None:
        logger.debug("[GoalParser] openai not available, skipping structured parsing")
        return enrich_goal_intent_for_verification(GoalIntent(raw_goal=goal))

    api_key = resolve_api_key(None, provider)
    if not api_key:
        logger.debug("[GoalParser] no API key, skipping structured parsing")
        return enrich_goal_intent_for_verification(GoalIntent(raw_goal=goal))

    _prov = normalize_provider(provider)
    base_url = resolve_base_url(_prov, os.environ.get("OPENAI_API_BASE"))
    llm_model = resolve_chat_model(_prov, model, base_url_override=os.environ.get("OPENAI_API_BASE"))

    try:
        resp = chat_completion_with_retry(
            model=llm_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"goal: {goal.strip()}"},
            ],
            temperature=0.0,
            timeout=None,
            provider=_prov,
            api_key=api_key,
            base_url=base_url,
            max_retries=2,
            timing_label="goal_parse",
        )
        content = (resp.choices[0].message.content or "").strip()
        intent = _parse_response(content, raw_goal=goal)
        intent = _sanitize_execution_constraints(intent, goal)
        _log_intent(intent, goal)
        return enrich_goal_intent_for_verification(intent)
    except Exception as exc:
        logger.warning("[GoalParser] LLM parse failed: %s", exc)
        return enrich_goal_intent_for_verification(GoalIntent(raw_goal=goal, confidence=0.0))


# ── Internal helpers ─────────────────────────────────────────────────────────

def _parse_response(content: str, *, raw_goal: str) -> GoalIntent:
    """将 LLM 返回文本解析为 GoalIntent，容错处理。"""
    # 去掉可能的 markdown 代码块包装
    stripped = content
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # 去掉首行（```json 或 ```）和尾行（```）
        inner_lines = lines[1:]
        if inner_lines and inner_lines[-1].strip() == "```":
            inner_lines = inner_lines[:-1]
        stripped = "\n".join(inner_lines)

    data: dict
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        # 尝试提取首个 {...} 块
        start = stripped.find("{")
        end = stripped.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(stripped[start:end])
            except json.JSONDecodeError:
                logger.warning("[GoalParser] JSON decode failed, returning raw goal")
                return enrich_goal_intent_for_verification(GoalIntent(raw_goal=raw_goal, confidence=0.0))
        else:
            logger.warning("[GoalParser] no JSON found in response, returning raw goal")
            return enrich_goal_intent_for_verification(GoalIntent(raw_goal=raw_goal, confidence=0.0))

    def _str_list(key: str) -> list[str]:
        val = data.get(key, [])
        if isinstance(val, list):
            return [str(v).strip() for v in val if v and str(v).strip()]
        return []

    confidence = data.get("confidence", 1.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 1.0
    confidence = max(0.0, min(1.0, confidence))

    return enrich_goal_intent_for_verification(
        GoalIntent(
            execution_constraints=_str_list("execution_constraints"),
            content_intent=_str_list("content_intent"),
            quality_requirements=_str_list("quality_requirements"),
            scope_constraints=_str_list("scope_constraints"),
            temporal_relationships=_str_list("temporal_relationships"),
            ambiguities=_str_list("ambiguities"),
            confidence=confidence,
            raw_goal=raw_goal,
        )
    )


def _log_intent(intent: GoalIntent, original_goal: str) -> None:
    """以结构化日志输出解析结果，方便调试。"""
    logger.info(
        "[GoalParser] goal parsed | confidence=%.2f | "
        "hard_constraints=%d | content_points=%d | ambiguities=%d",
        intent.confidence,
        len(intent.execution_constraints),
        len(intent.content_intent),
        len(intent.ambiguities),
    )
    if intent.execution_constraints:
        logger.info(
            "[GoalParser] execution_constraints: %s",
            intent.execution_constraints,
        )
    if intent.ambiguities:
        logger.warning(
            "[GoalParser] ambiguities detected for goal %r: %s",
            original_goal[:80],
            intent.ambiguities,
        )
