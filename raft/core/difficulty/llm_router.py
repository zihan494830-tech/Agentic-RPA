"""B2 可选：用 LLM 根据任务描述决定 single_flow / multi_flow，辅助编排层组织测试。"""
import json
import re

from raft.contracts.models import DifficultyRoutingResult, TaskSpec
from raft.core.llm_client import chat_completion_with_retry
from raft.core.llm_providers import resolve_agent_runtime, resolve_api_key

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore


def _build_routing_prompt(task_spec: TaskSpec) -> str:
    """构建发给 LLM 的 B2 路由 prompt（仅难度与单/多流，不含测试轮数；轮数由用户指定）。"""
    desc = task_spec.description or ""
    task_id = task_spec.task_spec_id or ""
    return (
        "你负责为 RPA 测试框架做「难度与路由」决策。根据以下任务信息：\n"
        f"任务 ID：{task_id}\n"
        f"任务描述：{desc}\n\n"
        "请只回复一个 JSON 对象，不要其他文字。格式：\n"
        "{\"route_type\": \"single_flow\" 或 \"multi_flow\", \"difficulty\": 0.0~1.0 的数字}\n\n"
        "规则：route_type 表示单流线性（single_flow）或多流/分支（multi_flow）；difficulty 为 0～1 的难度估计。\n"
    )


def _parse_routing_response(text: str, task_spec_id: str = "") -> DifficultyRoutingResult:
    """从 LLM 回复解析 route_type 与 difficulty（不再解析 suggested_rounds，轮数由用户指定）。"""
    text = text.strip()
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
        rt = data.get("route_type", "single_flow")
        if rt not in ("single_flow", "multi_flow"):
            rt = "single_flow"
        diff = float(data.get("difficulty", 0.0))
        diff = max(0.0, min(1.0, diff))
        return DifficultyRoutingResult(
            route_type=rt,
            difficulty=diff,
            suggested_rounds=None,
            extra={"task_spec_id": task_spec_id, "source": "llm"},
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return DifficultyRoutingResult(
            route_type="multi_flow",
            difficulty=0.0,
            suggested_rounds=None,
            extra={"task_spec_id": task_spec_id, "source": "llm_fallback"},
        )


class LLMRouter:
    """
    B2 可选：用 LLM 做难度与路由决策（single_flow / multi_flow），辅助 Orchestrator 组织测试。
    测试轮数由用户/脚本指定，不由 LLM 建议。
    支持 OpenAI、Qwen、Grok（xAI）等 OpenAI 兼容 API。
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
        self._api_key = resolve_api_key(api_key, provider)
        self._base_url, self.model = resolve_agent_runtime(provider, model, base_url)

    def __call__(self, task_spec: TaskSpec) -> DifficultyRoutingResult:
        """根据 TaskSpec 调用 LLM，返回 route_type 与难度。"""
        prompt = _build_routing_prompt(task_spec)
        resp = chat_completion_with_retry(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            timeout=60.0,
            provider=self._provider,
            api_key=self._api_key,
            base_url=self._base_url,
            max_retries=0,
            timing_label="b2_routing",
        )
        content = (resp.choices[0].message.content or "").strip()
        return _parse_routing_response(content, task_spec.task_spec_id)
