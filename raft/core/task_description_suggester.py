"""
单轮任务描述建议器：根据场景、待测 Agent 描述（及可选目标），用 LLM 生成一句「任务描述」。
供 B9 在 run 开始前调用（use_llm_task_description 时）；生成后再传给 Query 建议器。
支持 OpenAI、Qwen、Grok（xAI）等 OpenAI 兼容 API。
"""
import os
import re

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

from raft.core.llm_client import chat_completion_with_retry
from raft.core.llm_providers import normalize_provider, resolve_base_url, resolve_chat_model, resolve_api_key


def _build_prompt(scenario: str, agent_descriptor: str, goal: str | None = None) -> str:
    """构建发给 LLM 的 prompt。"""
    parts = [
        "你负责为 RPA 测试框架生成一句「任务描述」，用于说明本 run 要测什么。",
        "该描述会用于后续的「单轮/多轮」与测试轮数决策（难度与路由），请保持清晰、可判断。",
        f"场景：{scenario or '（未指定）'}",
        f"待测 Agent 描述：{agent_descriptor}",
    ]
    if goal and (goal := str(goal).strip()):
        parts.append(f"高层目标：{goal}")
    parts.append(
        "请根据上述信息，生成一句简洁的中文任务描述，明确本 run 的测试目的。"
        "只回复这一句描述，不要解释、不要引号、不要换行。长度控制在 40 字以内。"
    )
    return "\n".join(parts)


def _parse_description(text: str) -> str:
    """从 LLM 回复中提取单行任务描述。"""
    text = (text or "").strip()
    text = re.sub(r'^["\']|["\']$', "", text)
    text = text.split("\n")[0].strip()
    return text if text else ""


def suggest_task_description(
    scenario: str,
    agent_descriptor: str,
    goal: str | None = None,
    *,
    fallback: str = "在 Poffices 上完成一次 Query 测试",
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    provider: str | None = None,
) -> str:
    """
    根据场景、待测 Agent 描述及可选目标，调用 LLM 生成一句任务描述。
    返回非空字符串；若 LLM 不可用或失败，则返回 fallback。
    provider 可选：grok、qwen、或 None（OpenAI）。环境变量同 query_suggester。
    """
    if OpenAI is None:
        import warnings
        warnings.warn(
            "任务描述建议器：未安装 openai，使用回退描述。pip install openai 后可用 LLM 生成任务描述。",
            stacklevel=2,
        )
        return fallback
    _key = resolve_api_key(api_key, provider)
    if not _key:
        import warnings
        warnings.warn(
            "任务描述建议器：未设置 API Key，使用回退描述。请在 .env 中配置对应 provider 的 Key。",
            stacklevel=2,
        )
        return fallback

    _prov = normalize_provider(provider)
    _base = resolve_base_url(_prov, base_url)
    _model = resolve_chat_model(_prov, model, base_url_override=base_url)

    try:
        prompt = _build_prompt(scenario, agent_descriptor, goal)
        resp = chat_completion_with_retry(
            model=_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            timeout=None,
            provider=_prov,
            api_key=_key,
            base_url=_base,
            max_retries=3,
            timing_label="task_description",
        )
        content = (resp.choices[0].message.content or "").strip()
        desc = _parse_description(content)
        return desc if desc else fallback
    except Exception as e:
        import warnings
        warnings.warn(
            f"任务描述建议器 LLM 调用失败，使用回退描述: {e}",
            stacklevel=2,
        )
        return fallback
