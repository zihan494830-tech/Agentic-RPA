"""B6 决策组件：Poffices 场景下由 LLM 驱动 Block 决策，替换规则式 PofficesAgent。

LLM 收到当前 state + last_execution_result + 可用 Block 列表后，自主判断调用哪个 Block
以及传入什么参数（含 app_ready 的 options.agent_name，即**待测 Agent** = Poffices 页面产品），无需人工编写 if/elif 规则树。

LLM 不可用（未配置 API key / openai 未安装）或返回无法解析的响应时，自动 fallback 到
规则驱动的 PofficesAgent，确保可用性。

支持 Qwen（通义千问）、OpenAI、Grok 等 OpenAI 兼容 API，通过 provider 参数或 RAFT_LLM_PROVIDER 配置：
  - 默认：qwen，使用阿里云 DashScope（OPENAI_API_KEY / OPENAI_API_BASE / QWEN_MODEL）
  - provider="openai" / "grok"：按对应供应商解析密钥与 base
"""
import json
import logging
import os
import re

from raft.contracts.models import ToolCall
from raft.core.llm_client import chat_completion_with_retry
from raft.core.llm_providers import normalize_provider, resolve_agent_runtime, resolve_api_key

logger = logging.getLogger(__name__)

# 向 LLM 描述的可用 Block 清单（兜底：未配置 block_catalog 时使用）
_AVAILABLE_BLOCKS_LEGACY = [
    {
        "block_id": "poffices_bootstrap",
        "description": (
            "登录 Poffices 页面、选择 Market Analysis Agent 并点击 Apply，完成初始化。"
            "首次使用时调用；完成后 state.poffices_ready 变为 True。参数：无。"
        ),
        "params": {},
    },
    {
        "block_id": "poffices_query",
        "description": (
            "在已就绪的 Poffices 页面填写 query 并发送，等待生成完成后提取响应文本。"
            "必须在 state.poffices_ready 为 True 后才可调用。"
            "完成后 state.poffices_response 中可读到响应内容。"
        ),
        "params": {"query": "string，必填，要发送给 Market Analysis Agent 的查询内容"},
    },
]

_LEGACY_SYSTEM_PROMPT = (
    "你是一个 RPA 测试框架的 Agent 决策模块（B6）。\n"
    "你的职责是操作 Poffices 网页应用，测试其 Market Analysis Agent 的 Query 能力。\n\n"
    "【可用 Block 列表】\n"
    "{blocks}\n\n"
    "【决策指南（参考，可灵活判断）】\n"
    "1. state.poffices_ready 为 False 或不存在时 → 调用 poffices_bootstrap\n"
    "2. state.poffices_ready 为 True 且尚未成功执行过 poffices_query → 调用 poffices_query，"
    "   query 参数取自 state.query 或任务描述中的查询内容\n"
    "3. 上一步 poffices_query 成功（success=true）→ 任务完成，返回空 tool_calls\n"
    "4. 上一步失败且 error_type 为 timeout / rpa_execution_failed / element_not_found → 可重试同一 Block\n"
    "5. 其他失败（validation_error / missing_context 等）→ 返回空 tool_calls，停止\n\n"
    "【输出格式】\n"
    "只回复一个 JSON 对象，不要任何其他文字。\n"
    '格式：{{"tool_calls": [{{"tool_name": "block_id", "params": {{...}}}}]}}\n'
    '无需操作时返回：{{"tool_calls": []}}'
).format(blocks=json.dumps(_AVAILABLE_BLOCKS_LEGACY, ensure_ascii=False, indent=2))


def _build_decision_prompt(agent_input_context: dict, task_description: str) -> str:
    """构建每步决策的 user prompt：当前步数 + 状态 + 上一步结果。"""
    step = agent_input_context.get("current_step_index", 0)
    state = agent_input_context.get("state") or {}
    last_result = agent_input_context.get("last_execution_result")
    parts = [
        f"任务描述：{task_description or '在 Poffices 上测试 Agent 的 Query 能力'}",
        f"当前步数：{step}",
        f"当前状态：{json.dumps(state, ensure_ascii=False)}",
    ]
    if last_result:
        parts.append(f"上一步执行结果：{json.dumps(last_result, ensure_ascii=False)}")
    parts.append("请决定下一步调用哪个 Block（或停止）。")
    return "\n".join(parts)


def _parse_tool_calls(text: str) -> list[ToolCall] | None:
    """
    从 LLM 回复提取 tool_calls 列表。
    解析失败返回 None（调用方应 fallback），空列表 [] 表示主动停止。
    """
    text = text.strip()
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        raw = data.get("tool_calls")
        if not isinstance(raw, list):
            return None
        return [
            ToolCall(tool_name=t.get("tool_name", ""), params=t.get("params") or {})
            for t in raw
            if isinstance(t, dict) and t.get("tool_name")
        ]
    except (json.JSONDecodeError, TypeError):
        return None


class PofficesLLMAgent:
    """
    Poffices LLM 驱动决策组件：将每步 Block 决策权交给 LLM，以驱动对**待测 Agent**（Poffices 页面产品）的测试。

    与规则式 PofficesAgent 的区别：
    - 不再写死 if/elif 决策树，改由 LLM 根据 state + last_result 自主判断
    - 可自然扩展到多 Block、异常分支而无需修改代码
    - LLM 不可用或返回无效输出时自动 fallback，保证鲁棒性

    Args:
        model: 使用的模型名称（如 "deepseek-v3", "qwen-turbo"）
        api_key: API 密钥；未传则读取 OPENAI_API_KEY 环境变量
        base_url: API 基础 URL；未传则读取 OPENAI_API_BASE 或按 provider 使用默认值
        provider: 默认 qwen（DashScope）；可显式指定 openai / grok
        fallback_on_error: LLM 调用/解析失败时是否自动 fallback 到规则 Agent（默认 True）
        block_catalog: 可选。可用 Block 列表（通用语义），未传则使用旧版 poffices_bootstrap/poffices_query
        available_agents: 可选。本场景可选的 Agent 名称列表，供 LLM 在 app_ready 的 options.agent_name 中选择
        default_agent_name: 本 run 待测 Agent 名称（来自配置/CLI），写入 system prompt 并传给 fallback agent
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        provider: str | None = None,
        fallback_on_error: bool = True,
        block_catalog: list[dict] | None = None,
        available_agents: list[str] | None = None,
        default_agent_name: str | None = None,
    ) -> None:
        from raft.agents.poffices_agent import PofficesAgent

        self._provider = provider
        self._api_key = resolve_api_key(api_key, provider)
        self._default_agent_name = default_agent_name
        self._fallback_agent = PofficesAgent(default_agent_name=default_agent_name)
        self._fallback_on_error = fallback_on_error
        self._block_catalog = block_catalog if isinstance(block_catalog, list) else None
        self._available_agents = available_agents if isinstance(available_agents, list) else None

        prov = normalize_provider(provider)
        self._base_url, self.model = resolve_agent_runtime(prov, model, base_url)

    def _build_system_prompt(self) -> str:
        """根据是否配置 block_catalog 返回通用或旧版 system prompt。"""
        agent_fixed = ""
        if self._default_agent_name:
            agent_fixed = (
                f"\n【本 run 待测 Agent】\n"
                f"本次测试的 Agent 固定为「{self._default_agent_name}」。"
                f"首次调用 app_ready 时必须在 params.options 中传入 "
                f"{{\"agent_name\": \"{self._default_agent_name}\"}}。\n"
            )

        if self._block_catalog:
            blocks_json = json.dumps(self._block_catalog, ensure_ascii=False, indent=2)
            agents_hint = ""
            if self._available_agents:
                agents_hint = f"\n本场景可选 Agent：{json.dumps(self._available_agents, ensure_ascii=False)}。"
            return (
                "你是一个 RPA 测试框架的 Agent 决策模块（B6）。\n"
                "你的职责是操作 Poffices 网页应用，测试指定 Agent 的 Query 能力。\n\n"
                f"{agent_fixed}"
                "【可用 Block 列表】\n"
                f"{blocks_json}\n\n"
                "【决策指南（参考，可灵活判断）】\n"
                "1. state.app_ready / state.poffices_ready 为 False 或不存在时 → 调用 app_ready；"
                "在 params 中传 options.agent_name。\n"
                "2. 已就绪且尚未发送查询 → 调用 send_query，query 取自 state.query 或任务描述。\n"
                "3. 已发送查询但尚未取回结果 → 调用 get_response。\n"
                "4. 上一步 get_response 成功（success=true）→ 任务完成，返回空 tool_calls。\n"
                "5. 上一步失败且 error_type 为 timeout / rpa_execution_failed / element_not_found → 可重试同一 Block。\n"
                "6. 其他失败 → 返回空 tool_calls，停止。"
                f"{agents_hint}\n\n"
                "【输出格式】\n"
                "只回复一个 JSON 对象，不要任何其他文字。\n"
                '格式：{"tool_calls": [{"tool_name": "block_id", "params": {...}}]}\n'
                '无需操作时返回：{"tool_calls": []}'
            )
        return _LEGACY_SYSTEM_PROMPT

    def run(
        self,
        agent_input_context: dict,
        task_description: str = "",
    ) -> list[ToolCall]:
        """
        调用 LLM 决策下一步 Block；失败时自动 fallback 到规则 Agent。

        Returns:
            tool_calls 列表；空列表表示停止执行。
        """
        try:
            user_prompt = _build_decision_prompt(agent_input_context, task_description)
            logger.debug("[PofficesLLMAgent] user_prompt=%s", user_prompt)

            resp = chat_completion_with_retry(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._build_system_prompt()},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                timeout=30.0,
                provider=self._provider,
                api_key=self._api_key,
                base_url=self._base_url,
                max_retries=0,
                timing_label="b6_decision",
            )
            content = (resp.choices[0].message.content or "").strip()
            logger.debug("[PofficesLLMAgent] llm_response=%s", content)

            result = _parse_tool_calls(content)
            if result is not None:
                logger.info("[PofficesLLMAgent] LLM 决策 tool_calls=%s", result)
                return result

            logger.warning("[PofficesLLMAgent] LLM 返回无法解析，fallback 到规则 Agent。raw=%s", content)

        except Exception as exc:
            logger.warning("[PofficesLLMAgent] LLM 调用失败，fallback 到规则 Agent。reason=%s", exc)

        if self._fallback_on_error:
            return self._fallback_agent.run(agent_input_context, task_description)
        return []
