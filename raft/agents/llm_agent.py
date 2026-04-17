"""B6 单 Agent：可选接 LLM（OpenAI 兼容 API），根据 state + 最近 ExecutionResult 输出 tool_calls。支持 OpenAI、Qwen（通义千问）等。"""
import json
import re
from raft.contracts.models import ToolCall
from raft.core.llm_client import chat_completion_with_retry
from raft.core.llm_providers import normalize_provider, resolve_agent_runtime, resolve_api_key


def _build_prompt(agent_input_context: dict, task_description: str) -> str:
    """构建发给 LLM 的 prompt，含状态与最近执行结果。"""
    step = agent_input_context.get("current_step_index", 0)
    state = agent_input_context.get("state") or {}
    last_result = agent_input_context.get("last_execution_result")
    parts = [
        f"任务描述：{task_description}",
        f"当前步数：{step}",
        f"当前状态：{json.dumps(state, ensure_ascii=False)}",
    ]
    if last_result:
        parts.append(f"上一步执行结果：{json.dumps(last_result, ensure_ascii=False)}")
    parts.append(
        "请根据以上信息输出本步要执行的操作，以 JSON 格式回复，且只回复一个 JSON 对象，不要其他文字。"
        "格式：{\"tool_calls\": [{\"tool_name\": \"open_system\"|\"fetch_details\"|\"retry_operation\"|\"fill_form\"|\"click\", \"params\": {...}}]}。"
        "若无需再执行操作则返回 {\"tool_calls\": []}。"
    )
    return "\n".join(parts)


def _parse_tool_calls(text: str) -> list[ToolCall]:
    """从 LLM 回复中解析 tool_calls。"""
    text = text.strip()
    # 尝试提取 JSON 块
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
        raw = data.get("tool_calls") or []
        return [
            ToolCall(tool_name=t.get("tool_name", ""), params=t.get("params") or {})
            for t in raw
            if isinstance(t, dict) and t.get("tool_name")
        ]
    except (json.JSONDecodeError, TypeError):
        return []


class LLMAgent:
    """
    单 Agent：调用 OpenAI 兼容 API，根据 state + 最近 ExecutionResult 生成 tool_calls。
    支持 OpenAI、Qwen（通义千问）等。需设置环境变量 OPENAI_API_KEY；可选 OPENAI_API_BASE。
    使用 Qwen 时可直接传 provider=\"qwen\"，或设置 OPENAI_API_BASE 与 OPENAI_API_KEY。
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        provider: str | None = None,
    ) -> None:
        self._provider = provider
        prov = normalize_provider(provider)
        self._api_key = resolve_api_key(api_key, provider)
        self._base_url, self.model = resolve_agent_runtime(prov, model, base_url)

    def run(
        self,
        agent_input_context: dict,
        task_description: str = "",
    ) -> list[ToolCall]:
        """根据当前状态与最近 RPA 结果调用 LLM，解析并返回 tool_calls。"""
        prompt = _build_prompt(agent_input_context, task_description)
        resp = chat_completion_with_retry(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            provider=self._provider,
            api_key=self._api_key,
            base_url=self._base_url,
            max_retries=0,
            timing_label="b6_llm_agent",
        )
        content = (resp.choices[0].message.content or "").strip()
        return _parse_tool_calls(content)
