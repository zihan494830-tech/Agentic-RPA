"""
LLM 供应商与连接参数的统一解析（单一入口）。

换供应商时主要改环境变量（见下方「环境变量」）；业务代码应调用本模块的
`normalize_provider` / `resolve_base_url` / `resolve_chat_model` / `resolve_llm_connection`，
不要手写 qwen/grok/openai 分支与模型名。

支持：qwen（阿里云 DashScope 兼容）、grok（xAI）、openai（官方或其它 OpenAI 兼容端点）。
"""
from __future__ import annotations

import logging
import os
from typing import Final

logger = logging.getLogger(__name__)

# --- 默认端点与模型（可被环境变量覆盖）---

QWEN_BASE_URL: Final = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_DEFAULT_MODEL: Final = "deepseek-v3"

XAI_GROK_BASE_URL: Final = "https://api.x.ai/v1"
XAI_GROK_DEFAULT_MODEL: Final = "grok-beta"

OPENAI_OFFICIAL_DEFAULT_MODEL: Final = "gpt-4o-mini"

KNOWN_PROVIDERS: Final[frozenset[str]] = frozenset({"qwen", "grok", "openai", "azure", "siliconflow"})

SILICONFLOW_BASE_URL: Final = "https://api.siliconflow.cn/v1"
SILICONFLOW_PLANNER_MODEL: Final = "deepseek-ai/DeepSeek-R1"
SILICONFLOW_GENERAL_MODEL: Final = "deepseek-ai/DeepSeek-V3"
# 使用推理模型（R1）的 timing_label；其余使用通用模型（V3）
_SILICONFLOW_PLANNER_LABELS: Final[frozenset[str]] = frozenset({
    "goal_planner",
    "recovery_planner",
    "goal_parse",
    "goal_interpret",
})

AZURE_OPENAI_DEFAULT_API_VERSION: Final = "2024-12-01-preview"
AZURE_OPENAI_DEFAULT_DEPLOYMENT: Final = "o1-mini"
# o1/o1-mini 系列不支持 temperature 参数（需省略或固定为 1）
O1_MODEL_PREFIXES: Final[tuple[str, ...]] = ("o1", "o3")

"""
环境变量（与 .env 对应）：
  RAFT_LLM_PROVIDER   qwen | grok | openai
  OPENAI_API_BASE       可选；各供应商 OpenAI 兼容 base（勿带 /chat/completions）
  OPENAI_API_KEY        OpenAI / 百炼 DashScope 等兼容服务密钥
  XAI_API_KEY           xAI 密钥；RAFT_LLM_PROVIDER=grok 时优先于 OPENAI_API_KEY，否则次于 OPENAI_API_KEY
  QWEN_MODEL / XAI_MODEL / OPENAI_DEFAULT_MODEL
  RAFT_LLM_JUDGE_MODEL / RAFT_LLM_EXTRACT_MODEL（仅 openai 分支下作为备选模型名）
  RAFT_LLM_TIMEOUT      单次 LLM HTTP 超时（秒），默认 90；境外/高延迟 API 建议 120～180
"""


def http_timeout_seconds(override: float | None = None) -> float:
    """OpenAI SDK 的 request timeout（秒）。优先 override，否则读 RAFT_LLM_TIMEOUT，默认 90。"""
    if override is not None:
        return max(5.0, float(override))
    raw = (os.environ.get("RAFT_LLM_TIMEOUT") or "").strip()
    if raw:
        try:
            return max(5.0, float(raw))
        except ValueError:
            pass
    return 90.0


def normalize_provider(provider: str | None) -> str:
    """返回 qwen / grok / openai / azure；非法值回退为 qwen。"""
    p = (provider or os.environ.get("RAFT_LLM_PROVIDER") or "qwen").lower()
    if p not in KNOWN_PROVIDERS:
        return "qwen"
    return p


def resolve_siliconflow_model(timing_label: str | None = None) -> str:
    """按 timing_label 路由 SiliconFlow 模型：规划/恢复→R1（推理），其余→V3（通用）。"""
    if timing_label and timing_label in _SILICONFLOW_PLANNER_LABELS:
        return os.environ.get("SILICONFLOW_PLANNER_MODEL") or SILICONFLOW_PLANNER_MODEL
    return os.environ.get("SILICONFLOW_GENERAL_MODEL") or SILICONFLOW_GENERAL_MODEL


def is_o1_model(model: str | None) -> bool:
    """o1/o3 系列模型不支持 temperature，需调用方省略该参数。"""
    if not model:
        return False
    m = model.lower()
    return any(m.startswith(p) for p in O1_MODEL_PREFIXES)


def resolve_api_key(override: str | None = None, provider: str | None = None) -> str:
    """解析 API Key。显式 override 优先；否则按供应商选择环境变量顺序。

    grok：XAI_API_KEY → OPENAI_API_KEY；azure：AZURE_OPENAI_API_KEY → OPENAI_API_KEY；
    qwen / openai：OPENAI_API_KEY → XAI_API_KEY
    （避免已切到 Qwen 但本机仍留着 XAI_API_KEY 时误把 Grok 密钥发到百炼。）
    """
    o = (override or "").strip()
    if o:
        return o
    eff = normalize_provider(provider)
    if eff == "grok":
        return (os.environ.get("XAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if eff == "azure":
        return (os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if eff == "siliconflow":
        return (os.environ.get("SILICONFLOW_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    return (os.environ.get("OPENAI_API_KEY") or os.environ.get("XAI_API_KEY") or "").strip()


def resolve_base_url(provider: str | None, explicit: str | None = None) -> str | None:
    """
    解析 OpenAI SDK 的 base_url（不含 /chat/completions）。
    explicit 非空时优先；否则 OPENAI_API_BASE；再否则按供应商给默认（openai 为 None 表示官方 api.openai.com）。
    azure provider 返回 None（由 resolve_azure_params 单独处理）。
    """
    if explicit and explicit.strip():
        return explicit.strip()
    eff = normalize_provider(provider)
    if eff == "azure":
        return None  # Azure 不走 base_url，用 azure_endpoint 单独处理
    if eff == "siliconflow":
        return os.environ.get("SILICONFLOW_BASE_URL") or SILICONFLOW_BASE_URL
    env_b = (os.environ.get("OPENAI_API_BASE") or "").strip()
    if env_b:
        return env_b
    if eff == "grok":
        return XAI_GROK_BASE_URL
    if eff == "qwen":
        return QWEN_BASE_URL
    return None


def resolve_azure_params() -> tuple[str, str, str]:
    """返回 Azure OpenAI 专用三元组：(azure_endpoint, api_version, deployment_name)。"""
    endpoint = (os.environ.get("AZURE_OPENAI_ENDPOINT") or "").strip().rstrip("/")
    api_version = (os.environ.get("AZURE_OPENAI_API_VERSION") or AZURE_OPENAI_DEFAULT_API_VERSION).strip()
    deployment = (os.environ.get("AZURE_OPENAI_DEPLOYMENT") or AZURE_OPENAI_DEFAULT_DEPLOYMENT).strip()
    return endpoint, api_version, deployment


def default_model_for_openai_compatible_base(base_url: str | None) -> str:
    """provider=openai 或 base 未判明时：百炼域名用 QWEN 默认，否则 OpenAI 官方默认。"""
    b = (base_url or os.environ.get("OPENAI_API_BASE") or "").lower()
    if "dashscope" in b or "aliyuncs.com" in b:
        return os.environ.get("QWEN_MODEL") or QWEN_DEFAULT_MODEL
    return os.environ.get("OPENAI_DEFAULT_MODEL") or OPENAI_OFFICIAL_DEFAULT_MODEL


def sanitize_explicit_model(provider: str, model: str | None) -> str | None:
    """
    丢弃与当前供应商明显不兼容的显式 model，避免误把百炼/OpenAI 默认名发到 xAI 等。
    返回 None 表示调用方应改用环境变量/库默认。
    """
    if model is None or not str(model).strip():
        return None
    m = str(model).strip()
    eff = normalize_provider(provider)
    ml = m.lower()
    if eff == "grok":
        if ml.startswith("grok"):
            return m
        logger.warning(
            "[LLM] provider=grok 时忽略不兼容的 model=%s，改用 XAI_MODEL/默认",
            m,
        )
        return None
    if eff == "qwen":
        if ml.startswith("grok") or ml.startswith("gpt-"):
            logger.warning(
                "[LLM] provider=qwen 时忽略不兼容的 model=%s，改用 QWEN_MODEL/默认",
                m,
            )
            return None
        return m
    return m


def resolve_chat_model(
    provider: str | None,
    explicit: str | None = None,
    *,
    base_url_override: str | None = None,
    openai_env_fallbacks: tuple[str, ...] = (),
    qwen_env_fallbacks: tuple[str, ...] = (),
) -> str:
    """
    解析单次 Chat Completions 使用的 model 名称。
    openai_env_fallbacks：provider=openai 时按顺序尝试的环境变量名（如判分/抽取专用模型）。
    qwen_env_fallbacks：provider=qwen 时在 QWEN_MODEL 之前尝试的变量（如 RAFT_LLM_EXTRACT_MODEL）。
    """
    eff = normalize_provider(provider)
    bu = resolve_base_url(eff, base_url_override)

    if eff == "azure":
        m = explicit or os.environ.get("AZURE_OPENAI_DEPLOYMENT") or AZURE_OPENAI_DEFAULT_DEPLOYMENT
        return m

    if eff == "siliconflow":
        # 不在此处按 timing_label 路由（label 在 chat_completion_with_retry 层已有），
        # 显式传入时优先，否则返回通用模型作为占位（实际调用前会被 llm_client 覆盖）
        return explicit or os.environ.get("SILICONFLOW_GENERAL_MODEL") or SILICONFLOW_GENERAL_MODEL

    if eff == "grok":
        m = sanitize_explicit_model("grok", explicit)
        return m or os.environ.get("XAI_MODEL") or XAI_GROK_DEFAULT_MODEL

    if eff == "qwen":
        m = sanitize_explicit_model("qwen", explicit)
        if m:
            return m
        for k in qwen_env_fallbacks:
            raw = os.environ.get(k)
            if raw:
                m = sanitize_explicit_model("qwen", raw)
                if m:
                    return m
        return os.environ.get("QWEN_MODEL") or QWEN_DEFAULT_MODEL

    # openai
    m = explicit
    if not m:
        for k in openai_env_fallbacks:
            m = os.environ.get(k)
            if m:
                break
    if not m:
        m = default_model_for_openai_compatible_base(bu)
    return m


def resolve_llm_connection(
    *,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    openai_env_fallbacks: tuple[str, ...] = (),
    qwen_env_fallbacks: tuple[str, ...] = (),
) -> tuple[str, str, str | None, str]:
    """
    解析完整连接四元组：(api_key, model, base_url, effective_provider)。
    与历史 `llm_client._resolve_llm_config` 行为一致，并统一 sanitize。
    """
    eff = normalize_provider(provider)
    key = resolve_api_key(api_key, provider)
    bu = resolve_base_url(eff, base_url)
    m = resolve_chat_model(
        eff,
        model,
        base_url_override=base_url,
        openai_env_fallbacks=openai_env_fallbacks,
        qwen_env_fallbacks=qwen_env_fallbacks,
    )
    return (key, m, bu, eff)


def resolve_openai_client_params(
    provider: str | None,
    model: str | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    openai_env_fallbacks: tuple[str, ...] = (),
    qwen_env_fallbacks: tuple[str, ...] = (),
) -> tuple[str, str | None, str]:
    """
    供直接构造 OpenAI() 的模块使用（judge、extract、B2 router）。
    返回 (api_key, base_url, resolved_model)；无 key 时 api_key 为空字符串。
    """
    eff = normalize_provider(provider)
    key = resolve_api_key(api_key, provider)
    bu = resolve_base_url(eff, base_url)
    m = resolve_chat_model(
        eff,
        model,
        base_url_override=base_url,
        openai_env_fallbacks=openai_env_fallbacks,
        qwen_env_fallbacks=qwen_env_fallbacks,
    )
    return (key, bu, m)


def resolve_agent_runtime(
    provider: str | None,
    model: str | None = None,
    base_url: str | None = None,
) -> tuple[str | None, str]:
    """
    PofficesLLMAgent / LLMAgent / LLMRouter：返回 (base_url, model)。
    """
    eff = normalize_provider(provider)
    bu = resolve_base_url(eff, base_url)
    m = resolve_chat_model(eff, model, base_url_override=base_url)
    return (bu, m)
