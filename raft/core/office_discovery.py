"""
Office Discovery：动态从 Poffices UI 发现 office 与 agent，供 goal 驱动流程使用。

流程：list_offices → match_office → expand_office → list_agents_in_office → select_agents_for_topic
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

from raft.contracts.models import ToolCall
from raft.core.goal_interpreter import GoalIntent
from raft.core.llm_client import chat_completion_with_retry

logger = logging.getLogger(__name__)

_OFFICE_ROW_RE = re.compile(
    r"^.+?\s*\(\s*\d+\s*/\s*\d+\s*selected\s*\)\s*$",
    re.IGNORECASE | re.UNICODE,
)


def filter_agent_candidates(
    raw: list[str],
    *,
    office_name: str | None = None,
) -> list[str]:
    """
    去掉 Office 分组名与侧栏 Office 行（含 x/y selected），只保留真正的 Agent 名称候选项。
    「HR Office」等是类别/分组，不是 Agent，不得进入 select_agents_for_topic。
    """
    on = (office_name or "").strip().lower()
    out: list[str] = []
    seen: set[str] = set()
    for a in raw:
        s = (a or "").strip()
        if not s or len(s) < 2:
            continue
        low = s.lower()
        if on and low == on:
            continue
        if _OFFICE_ROW_RE.match(s):
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def match_office(office_intent: str, discovered_offices: list[str], provider: str = "qwen") -> str | None:
    """
    从用户意图（如 research、business）匹配 UI 中的 office 名称。
    返回匹配到的 office 名，若无则返回 None。
    """
    if not office_intent or not discovered_offices:
        return None
    intent = office_intent.strip().lower()
    for o in discovered_offices:
        if not o or not isinstance(o, str):
            continue
        ol = o.lower()
        if intent in ol or ol in intent:
            return o
        if "research" in intent and "research" in ol:
            return o
        if "business" in intent and "business" in ol:
            return o
        if "strategy" in intent and "strategy" in ol:
            return o
        if "hr" in intent and "hr" in ol:
            return o
        if "marketing" in intent and "marketing" in ol:
            return o

    # 输入意图无法与已知 office 关键词建立弱匹配时，避免触发 LLM 猜测导致测试/流程不确定。
    if not any(k in intent for k in ("research", "business", "strategy", "hr", "marketing")):
        return None

    if OpenAI and discovered_offices:
        try:
            prompt = f"""从以下 office 列表中，选出与用户意图「{office_intent}」最匹配的一个。只返回 office 名称，不要其他文字。

Office 列表：{json.dumps(discovered_offices)}

返回格式：直接返回 office 名称，如 "Research Office"
"""
            resp = chat_completion_with_retry(
                model=None,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                timeout=None,
                provider=provider,
                max_retries=1,
                timing_label="discovery_match_office",
            )
            text = (resp.choices[0].message.content or "").strip()
            match = re.search(r'"([^"]+)"', text) or re.search(r"(\w[\w\s]+Office)", text)
            if match:
                name = match.group(1).strip()
                if name in discovered_offices:
                    return name
            for o in discovered_offices:
                if o in text or text in o:
                    return o
        except Exception as e:
            logger.warning("match_office LLM 失败: %s", e)
    return None


def select_agents_for_topic(
    discovered_agents: list[str],
    topic: str,
    count: int,
    provider: str = "qwen",
) -> list[str]:
    """
    从 discovered_agents 中选出 count 个最适合 topic 的 **Agent**（非 Office 类别名）。
    """
    if not discovered_agents or count <= 0:
        return []
    discovered_agents = [a for a in discovered_agents if a and isinstance(a, str) and len(a.strip()) >= 2]
    if not discovered_agents:
        return []
    if count >= len(discovered_agents):
        return discovered_agents[:count]

    if OpenAI:
        try:
            prompt = f"""从下列列表中选出 {count} 个最适合完成「{topic}」相关任务的 **Agent**（具体智能体名称）。只返回 JSON 数组。

列表：{json.dumps(discovered_agents)}

规则：每一项必须是「Agent」名称；**禁止**选择 Office 分组名（如 "HR Office"、"Research Office"）、禁止选择仅表示类别的词。若列表中混入 Office 名，必须忽略。

返回格式：["Agent1", "Agent2", ...]
"""
            resp = chat_completion_with_retry(
                model=None,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                timeout=None,
                provider=provider,
                max_retries=1,
                timing_label="discovery_select_agents",
            )
            text = (resp.choices[0].message.content or "").strip()
            match = re.search(r"\[[\s\S]*\]", text)
            if match:
                arr = json.loads(match.group(0))
                if isinstance(arr, list):
                    selected = [str(a).strip() for a in arr if a and str(a).strip() in discovered_agents]
                    return selected[:count]
        except Exception as e:
            logger.warning("select_agents_for_topic LLM 失败: %s", e)

    return discovered_agents[:count]


def infer_office_from_topic(
    topic: str,
    goal: str,
    *,
    discovered_offices: list[str] | None = None,
    provider: str = "qwen",
) -> str:
    """
    当用户未指定 office 时，从 topic 和 goal 推断应使用哪个 office。
    返回 office_intent 关键词（如 research、strategy、hr、marketing、business、financial 等）。
    """
    text = f"{topic or ''} {goal or ''}".strip()
    if not text:
        return "research"  # 默认

    # 规则兜底：常见 topic 关键词 → office
    text_lower = text.lower()
    rules = [
        (["研究", "research", "proposal", "文献", "方法论", "实验", "风险分析", "评估报告"], "research"),
        (["策略", "战略", "strategy", "危机", "谈判", "esg", "可持续"], "strategy"),
        (["招聘", "人力", "hr", "员工", "绩效", "培训", "考核"], "hr"),
        (["市场", "营销", "marketing", "广告", "销售", "活动", "展会"], "marketing"),
        (["财务", "投资", "financial", "预算", "投资组合", "融资"], "financial"),
        (["业务", "business", "运营", "供应链", "采购"], "business"),
        (["项目", "project", "研发", "开发计划"], "project"),
        (["数据", "data"], "data"),
    ]
    for keywords, office in rules:
        if any(k in text_lower for k in keywords):
            return office

    # LLM 推断
    if OpenAI and discovered_offices:
        try:
            prompt = f"""根据用户目标推断应使用哪个 Office 的 agents。只返回一个英文关键词。

用户目标：{goal}
主题：{topic or '(无)'}

可选 Office 类型：research, strategy, hr, marketing, financial, business, project, data, pr, education

返回格式：直接返回一个词，如 research
"""
            resp = chat_completion_with_retry(
                model=None,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                timeout=None,
                provider=provider,
                max_retries=1,
                timing_label="discovery_infer_office",
            )
            out = (resp.choices[0].message.content or "").strip().lower()
            for o in discovered_offices:
                if out in (o or "").lower():
                    return out
            if out in ("research", "strategy", "hr", "marketing", "financial", "business", "project", "data", "pr", "education"):
                return out
        except Exception as e:
            logger.warning("infer_office_from_topic LLM 失败: %s", e)

    return "research"


def run_discovery(
    rpa: Any,
    goal_intent: GoalIntent,
    *,
    provider: str = "qwen",
    allowed_agents: list[str] | None = None,
) -> list[str]:
    """
    执行 Discovery 流程：bootstrap → list_offices → expand_office → list_agents_in_office → select_agents。
    若用户未指定 office（office_intent 为空），则从 topic/goal 自动推断。
    以 UI 发现为准，不使用 allowed_agents 过滤，避免后台新增 agent 后场景名单未更新导致无法执行。

    返回选中的 agent 列表。
    """
    # 需有 office_intent、topic/raw_goal 或 count 才能跑 Discovery；office_intent 为空时从 topic 推断
    if not goal_intent.office_intent and not (goal_intent.topic or goal_intent.raw_goal) and goal_intent.count < 1:
        return []

    try:
        rpa._ensure_page()
    except Exception as e:
        logger.warning("Discovery: 无法获取 page: %s", e)
        return []

    offices: list[str] = []
    agents: list[str] = []

    try:
        er = rpa.execute(0, ToolCall(tool_name="discovery_bootstrap", params={}))
        if not er.success:
            logger.warning("Discovery: discovery_bootstrap 失败")
            return []

        er = rpa.execute(1, ToolCall(tool_name="list_offices", params={}))
        if er.success and isinstance(er.raw_response, dict):
            offices = er.raw_response.get("offices") or []
        if not offices:
            logger.warning("Discovery: list_offices 未获取到 offices")
            return []

        office_intent = goal_intent.office_intent
        if not office_intent:
            office_intent = infer_office_from_topic(
                goal_intent.topic,
                goal_intent.raw_goal,
                discovered_offices=offices,
                provider=provider,
            )
            logger.info("Discovery: 未指定 office，从 topic 推断得 office_intent=%s", office_intent)

        matched = match_office(office_intent, offices, provider=provider)
        if not matched:
            logger.warning("Discovery: 无法匹配 office_intent=%s 到 %s", office_intent, offices)
            return []

        er = rpa.execute(2, ToolCall(tool_name="expand_office", params={"office_name": matched}))
        if not er.success:
            logger.warning("Discovery: expand_office 失败")
            return []

        er = rpa.execute(
            3,
            ToolCall(
                tool_name="list_agents_in_office",
                params={"office_name": matched},
            ),
        )
        if er.success and isinstance(er.raw_response, dict):
            agents = er.raw_response.get("agents") or []
        if not agents:
            logger.warning("Discovery: list_agents_in_office 未获取到 agents")
            return []

        agents = filter_agent_candidates(agents, office_name=matched)
        if not agents:
            logger.warning(
                "Discovery: 过滤 Office 类目后无可用 Agent 候选项（office=%s）",
                matched,
            )
            return []

        # 以 UI 发现为准，不按 scenario.allowed_agents 过滤，避免后台新增 agent 后场景未更新导致无法执行
        # （校验阶段会将 Discovery 结果并入 allowed，故不会因不在名单而报错）

        selected = select_agents_for_topic(
            agents,
            goal_intent.topic or "general",
            goal_intent.count,
            provider=provider,
        )
        return selected

    except Exception as e:
        logger.warning("Discovery 异常: %s", e)
        return []
