"""
Goal Interpreter：将用户一句话解析为结构化意图。

输入：用户自然语言 goal
输出：GoalIntent（agents、topic、flow_type、scenario_id 等）

供 run_poffices_agent.py 等入口在加载 experiment 前调用，实现「用户说一句话 → 系统自动推演」。
支持 OpenAI、Qwen、Grok（xAI）等 OpenAI 兼容 API。API Key 与项目其它 LLM 共用（OPENAI_API_KEY）。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

from raft.core.llm_client import chat_completion_with_retry
from raft.core.llm_providers import normalize_provider, resolve_base_url, resolve_chat_model, resolve_api_key

logger = logging.getLogger(__name__)

# 无 available_agents 时的兜底列表（应由调用方从 scenario.allowed_agents 传入）
_FALLBACK_AGENTS = [
    "Research Proposal",
    "Market Analysis",
    "Project Proposal",
    "Marketing Plan",
    "Business Forecasting Objective",
]


@dataclass
class GoalIntent:
    """用户 goal 解析后的结构化意图。"""
    agents: list[str] = field(default_factory=list)
    topic: str = ""
    flow_type: str = "single_agent"
    scenario_id: str = "poffices-agent"
    raw_goal: str = ""
    office_intent: str = ""  # 用户意图中的 office（如 research、business），用于 Discovery 匹配
    count: int = 1  # 用户要的 agent 数量（如「三个」→ 3）
    collaboration_mode: bool = False  # True=多 Agent 协作产出一份报告；False=多 Agent 各自测试
    runs: int = 1  # 用户要跑的轮数（如「跑 3 轮」「多轮」→ 3；「单轮」→ 1）
    runs_per_agent: int = 1  # 多 Agent 时每个 agent 跑的轮数（如「每个 agent 跑两轮」→ 2）

    def to_extra_overrides(self) -> dict:
        """转为可用于 experiment.extra 的覆盖项。"""
        out: dict = {
            "goal": self.raw_goal or "",
        }
        if self.agents:
            out["agents_to_test"] = self.agents
        if self.topic:
            out["topic"] = self.topic
        if self.collaboration_mode:
            out["collaboration_mode"] = True
        if self.runs >= 1:
            out["runs"] = self.runs
        if self.runs_per_agent >= 1 and len(self.agents) > 1:
            out["runs_per_agent"] = self.runs_per_agent
        return out


def _build_prompt(goal: str, available_agents: list[str] | None = None) -> str:
    agents = available_agents or _FALLBACK_AGENTS
    agents_text = "、".join(agents[:30]) + ("…" if len(agents) > 30 else "")
    return f"""你负责解析 RPA 测试框架中用户的「目标描述」，输出结构化 JSON。

用户目标：{goal}

可用 Agent 列表（仅从中选择，不得编造；若为空则 agents 留空，由 Discovery 阶段从 UI 发现）：{agents_text}

重要：
- **agents** 中每一项必须是「可用 Agent 列表」里的**完整原名**（如 "Research Proposal"）。不得把用户描述的主题、任务句、Office 名（如 "HR Office"、"HR office daily schedule"）写进 agents。
- 若用户只描述**领域/主题/office 区域**（如人力资源、日程、HR 日常）而未点名具体 Agent，**agents 必须为空数组 []**，并填写 **office_intent**（如 hr）与 **topic**（主题描述），由 Discovery 从 UI 展开 Office 再选 Agent。

请输出 JSON，格式如下（不要输出其他文字）：
```json
{{
  "agents": ["Agent 名称1", "Agent 名称2"],
  "topic": "主题/关键词（如 openclaw、石油价格等，空字符串表示无特定主题）",
  "flow_type": "single_agent 或 multi_agent_linear",
  "scenario_id": "poffices-agent",
  "office_intent": "用户意图中的 office 类别（如 research、business、strategy、hr、marketing 等，用于后续从 UI 匹配；无则空字符串）",
  "count": 用户要的 agent 数量（如「三个」→ 3，「多个」→ 3，「一个」→ 1；未明确时 1）,
  "output_type": "single_report 或 multi_report",
  "runs": 用户要跑的轮数（如「跑 3 轮」「多轮测试」→ 3；「单轮」「一次」→ 1；未明确时 1）,
  "runs_per_agent": 仅当 agents>1 且用户明确说「每个 agent 跑 N 轮」「每个 agent 测 N 次」时填写 N；否则不填或 1。例：「两个 agent 每个跑两轮」→ 2
}}
```

四种运行模式（根据用户意图语义理解，自主判断属于哪种）：
1. 单 Agent 多轮：agents=1，runs>1。同一 Agent 跑多轮，每轮不同 query（可深入提问）。
2. 多 Agent 协作：agents>1，output_type=single_report。多个 Agent 协作产出一份报告。
3. 一次性测多个 Agent：agents>1，output_type=multi_report，runs_per_agent=1。每轮依次测每个 Agent。
4. 多 Agent 每 agent 多轮：agents>1，runs_per_agent>1。每个 agent 先测完 N 轮，再测下一个；顺序为 Agent1×N→Agent2×N。

解析原则：根据用户目标整体语义理解。「每个 agent 跑两轮」→ runs_per_agent=2；「跑 3 轮」→ runs=3；「让三个 agent 一起写」→ single_report；「对比三个 agent」→ multi_report。
"""


def _parse_llm_response(text: str) -> dict | None:
    text = (text or "").strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        text = match.group(1).strip()
    else:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            text = match.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def interpret_goal(
    goal: str,
    *,
    available_agents: list[str] | None = None,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> GoalIntent:
    """
    将用户自然语言 goal 解析为结构化 GoalIntent。

    返回 GoalIntent；若 LLM 不可用或解析失败，返回 fallback（保留原始 goal，agents 为空）。
    provider 可选：grok、qwen、或 None（OpenAI）。环境变量同 task_description_suggester。
    """
    goal = (goal or "").strip()
    fallback = GoalIntent(raw_goal=goal, scenario_id="poffices-agent")

    if OpenAI is None:
        logger.warning("Goal Interpreter：未安装 openai，使用 fallback。")
        return fallback

    _key = resolve_api_key(api_key, provider)
    if not _key:
        logger.warning("Goal Interpreter：未设置 OPENAI_API_KEY，使用 fallback。")
        return fallback

    _provider = normalize_provider(provider)
    _base = resolve_base_url(_provider, base_url)
    _model = resolve_chat_model(_provider, model, base_url_override=base_url)

    try:
        prompt = _build_prompt(goal, available_agents=available_agents or _FALLBACK_AGENTS)
        resp = chat_completion_with_retry(
            model=_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            timeout=None,
            provider=_provider,
            api_key=_key,
            base_url=_base,
            max_retries=2,
            timing_label="goal_interpret",
        )
        content = (resp.choices[0].message.content or "").strip()
        data = _parse_llm_response(content)
        if not isinstance(data, dict):
            return fallback

        agents = data.get("agents")
        if isinstance(agents, list):
            agents = [str(a).strip() for a in agents if a and str(a).strip()]
        else:
            agents = []

        # 仅保留场景白名单中的 Agent 名；误填的主题/句子会落在这里，必须剔除以走 Discovery
        _allow = available_agents if available_agents else _FALLBACK_AGENTS
        _allow_set = {a.strip() for a in _allow if isinstance(a, str) and a.strip()}
        if _allow_set:
            _before = list(agents)
            agents = [a for a in agents if a in _allow_set]
            if _before and agents != _before:
                logger.info(
                    "Goal Interpreter：已剔除非白名单 Agent，原=%s 保留=%s",
                    _before,
                    agents,
                )

        topic = str(data.get("topic") or "").strip()
        flow_type = str(data.get("flow_type") or "single_agent").strip()
        if flow_type not in ("single_agent", "multi_agent_linear"):
            flow_type = "multi_agent_linear" if len(agents) > 1 else "single_agent"

        scenario_id = str(data.get("scenario_id") or "poffices-agent").strip()
        office_intent = str(data.get("office_intent") or "").strip()
        output_type = str(data.get("output_type") or "").strip().lower()
        collaboration_mode = output_type == "single_report"
        count_raw = data.get("count")
        count = 1
        if isinstance(count_raw, int) and count_raw >= 1:
            count = min(count_raw, 10)
        elif isinstance(count_raw, (float, str)):
            try:
                count = min(max(1, int(float(count_raw))), 10)
            except (ValueError, TypeError):
                pass

        runs_raw = data.get("runs")
        runs = 1
        if isinstance(runs_raw, int) and runs_raw >= 1:
            runs = min(runs_raw, 20)
        elif isinstance(runs_raw, (float, str)):
            try:
                runs = min(max(1, int(float(runs_raw))), 20)
            except (ValueError, TypeError):
                pass

        runs_per_agent_raw = data.get("runs_per_agent")
        runs_per_agent = 1
        if isinstance(runs_per_agent_raw, int) and runs_per_agent_raw >= 1:
            runs_per_agent = min(runs_per_agent_raw, 10)
        elif isinstance(runs_per_agent_raw, (float, str)):
            try:
                runs_per_agent = min(max(1, int(float(runs_per_agent_raw))), 10)
            except (ValueError, TypeError):
                pass

        return GoalIntent(
            agents=agents,
            topic=topic,
            flow_type=flow_type,
            scenario_id=scenario_id or "poffices-agent",
            raw_goal=goal,
            office_intent=office_intent,
            count=count,
            collaboration_mode=collaboration_mode,
            runs=runs,
            runs_per_agent=runs_per_agent,
        )
    except Exception as e:
        logger.warning("Goal Interpreter LLM 调用失败，使用 fallback: %s", e)
        return fallback
