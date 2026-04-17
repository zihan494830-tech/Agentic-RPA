"""
LLM 调用统一封装：支持 OpenAI、Qwen、Grok 等兼容 API，带重试与超时。

连接参数解析见 `raft.core.llm_providers`（换供应商时改环境与该模块即可）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from raft.core.llm_providers import (
    OPENAI_OFFICIAL_DEFAULT_MODEL,
    QWEN_BASE_URL,
    QWEN_DEFAULT_MODEL,
    XAI_GROK_BASE_URL,
    XAI_GROK_DEFAULT_MODEL,
    default_model_for_openai_compatible_base,
    http_timeout_seconds,
    is_o1_model,
    normalize_provider,
    resolve_azure_params,
    resolve_llm_connection,
    resolve_siliconflow_model,
)
from raft.core.llm_timing import record_llm_api_call

try:
    from openai import AzureOpenAI, OpenAI
except ImportError:
    OpenAI = None  # type: ignore
    AzureOpenAI = None  # type: ignore

logger = logging.getLogger(__name__)

# 兼容旧 import：业务代码可能 `from llm_client import QWEN_BASE_URL` 等
__all__ = [
    "QWEN_BASE_URL",
    "QWEN_DEFAULT_MODEL",
    "XAI_GROK_BASE_URL",
    "XAI_GROK_DEFAULT_MODEL",
    "OPENAI_OFFICIAL_DEFAULT_MODEL",
    "_default_model_openai_branch",
    "build_openai_client",
    "chat_completion_with_retry",
    "chat_completion_safe",
]

# 历史名称保留
_default_model_openai_branch = default_model_for_openai_compatible_base


def _resolve_llm_config(
    *,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> tuple[str, str, str | None, str]:
    return resolve_llm_connection(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
    )


def chat_completion_with_retry(
    *,
    model: str | None = None,
    messages: list[dict[str, Any]],
    temperature: float = 0.3,
    timeout: float | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    max_retries: int = 3,
    retry_delay_base: float = 1.0,
    timing_label: str | None = None,
    max_tokens: int | None = None,
) -> Any:
    """
    调用 OpenAI 兼容 API 的 chat completion，带指数退避重试。

    Args:
        model: 模型名；未传时按 `llm_providers.resolve_chat_model` 解析
        provider: qwen / grok / openai；未传时读 RAFT_LLM_PROVIDER
        timeout: HTTP 超时（秒）；None 时使用 `RAFT_LLM_TIMEOUT`（默认 90）
    """
    if OpenAI is None:
        raise ImportError("openai 未安装，请 pip install openai")

    _to = http_timeout_seconds(timeout)

    _key, _model, _base, _eff_provider = _resolve_llm_config(
        provider=provider, api_key=api_key, base_url=base_url, model=model
    )
    if not _key:
        raise ValueError("未设置 OPENAI_API_KEY / AZURE_OPENAI_API_KEY 或 XAI_API_KEY")

    # Azure OpenAI 使用 AzureOpenAI 客户端
    if normalize_provider(provider) == "azure":
        if AzureOpenAI is None:
            raise ImportError("openai 未安装，请 pip install openai")
        _az_endpoint, _az_api_version, _az_deployment = resolve_azure_params()
        if not _az_endpoint:
            raise ValueError("Azure provider 需设置 AZURE_OPENAI_ENDPOINT")
        client = AzureOpenAI(
            api_key=_key,
            azure_endpoint=_az_endpoint,
            api_version=_az_api_version,
        )
        _model = _az_deployment
    else:
        if OpenAI is None:
            raise ImportError("openai 未安装，请 pip install openai")
        kwargs: dict[str, Any] = {"api_key": _key}
        if _base:
            kwargs["base_url"] = _base
        client = OpenAI(**kwargs)

    # SiliconFlow：按 timing_label 路由推理/通用模型（覆盖调用方传入的 model）
    if normalize_provider(provider) == "siliconflow":
        _model = resolve_siliconflow_model(timing_label)
        logger.debug("[LLMClient] SiliconFlow routing: label=%s → model=%s", timing_label, _model)

    last_exc: Exception | None = None
    call_started = time.perf_counter()
    create_kw: dict[str, Any] = {
        "model": _model,
        "messages": messages,
        "timeout": _to,
    }
    # o1/o3 系列不支持 temperature 参数，其余模型正常传入
    if not is_o1_model(_model):
        create_kw["temperature"] = temperature
    if max_tokens is not None:
        # o1 系列用 max_completion_tokens，其余用 max_tokens
        tok_key = "max_completion_tokens" if is_o1_model(_model) else "max_tokens"
        create_kw[tok_key] = max_tokens
    for attempt in range(max_retries + 1):
        try:
            resp = client.chat.completions.create(**create_kw)
            if attempt > 0:
                logger.info("[LLMClient] 重试第 %d 次成功", attempt)
            total_ms = int((time.perf_counter() - call_started) * 1000)
            record_llm_api_call(total_ms, timing_label)
            return resp
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                delay = retry_delay_base * (2**attempt)
                logger.warning(
                    "[LLMClient] 第 %d 次调用失败: %s，%s 秒后重试",
                    attempt + 1,
                    e,
                    delay,
                )
                time.sleep(delay)
            else:
                logger.warning("[LLMClient] 已达最大重试次数 %d，放弃", max_retries + 1)

    raise last_exc  # type: ignore[misc]


def build_openai_client(
    provider: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    openai_env_fallbacks: tuple[str, ...] = (),
    qwen_env_fallbacks: tuple[str, ...] = (),
) -> tuple[Any, str]:
    """
    返回 (OpenAI client 或 None, model_name)。
    key 未配置时返回 (None, model_name)，调用方可据此跳过 LLM 调用。
    openai_env_fallbacks / qwen_env_fallbacks 传递给 resolve_openai_client_params 用于特定供应商的模型环境变量兜底。
    供 llm_judge、llm_extract 等直接构造 client 的模块共用，避免重复实现。
    """
    from raft.core.llm_providers import resolve_openai_client_params

    if OpenAI is None:
        return (None, model or QWEN_DEFAULT_MODEL)
    _key, _base, _model = resolve_openai_client_params(
        provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        openai_env_fallbacks=openai_env_fallbacks,
        qwen_env_fallbacks=qwen_env_fallbacks,
    )
    if not _key:
        return (None, _model)
    kwargs: dict[str, Any] = {"api_key": _key}
    if _base:
        kwargs["base_url"] = _base
    return (OpenAI(**kwargs), _model)


def chat_completion_safe(
    *,
    model: str | None = None,
    messages: list[dict[str, Any]],
    temperature: float = 0.3,
    timeout: float | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    max_retries: int = 3,
    timing_label: str | None = None,
) -> str | None:
    """
    安全版：调用失败时返回 None，不抛异常。
    返回 choices[0].message.content 或 None。
    """
    try:
        resp = chat_completion_with_retry(
            model=model,
            messages=messages,
            temperature=temperature,
            timeout=timeout,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            max_retries=max_retries,
            timing_label=timing_label,
        )
        content = (resp.choices[0].message.content or "").strip()
        return content if content else None
    except Exception as e:
        logger.warning("[LLMClient] chat_completion_safe 失败: %s", e)
        return None
