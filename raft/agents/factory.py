"""Agent 工厂：根据 ExperimentConfig.extra 中的 agent_type 字段创建对应 Agent 实例。

支持的 agent_type 值：
  "llm"   → PofficesLLMAgent（LLM 驱动决策，LLM 不可用时自动 fallback 到规则 Agent）
  "rule"  → PofficesAgent（纯规则驱动，默认兜底值）

experiment_poffices.json 中可配置：
  "agent_type": "llm"          # 启用 LLM 决策
  "agent_provider": "qwen"     # LLM 提供商（可选，默认与 RAFT_LLM_PROVIDER 一致，常为 qwen）
  "agent_model": "deepseek-v3"  # 模型名称（可选；百炼默认见 QWEN_MODEL / llm_client）
  "agent_under_test": "Research Proposal"  # 本 run 默认待测 Agent

运行脚本可通过 CLI 参数覆盖配置（cli_override 优先级最高）：
  --llm-agent          → 强制使用 LLM Agent
  --no-llm-agent       → 强制使用规则 Agent（无视配置）
  --llm-provider qwen  → 覆盖提供商
  --llm-model  xxx     → 覆盖模型
  --agent "名称"       → 覆盖待测 Agent（任意名称，直接信任 CLI 输入）
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raft.contracts.models import ExperimentConfig

from raft.core.config.scenario import resolve_allowed_agents, resolve_block_catalog, resolve_suggested_agents

logger = logging.getLogger(__name__)


def resolve_agent_under_test(config: "ExperimentConfig", cli_agent: str | None = None) -> str:
    """按优先级决定本 run 待测 Agent 名称：CLI > extra.agent_under_test > 默认值。

    CLI 传入时直接信任（不校验名单），允许测试任意 Agent。
    未传 CLI 时使用配置默认或 "Research Proposal"。
    """
    if cli_agent:
        return cli_agent

    extra = config.extra or {}
    configured = extra.get("agent_under_test")
    if isinstance(configured, str) and configured.strip():
        return configured

    suggested = resolve_suggested_agents(config)
    if suggested:
        return suggested[0]
    return "Research Proposal"


def create_poffices_agent(
    config: "ExperimentConfig",
    *,
    cli_agent_type: str | None = None,
    cli_provider: str | None = None,
    cli_model: str | None = None,
    default_agent_name: str | None = None,
) -> object:
    """
    根据实验配置和可选的 CLI 覆盖参数，创建并返回合适的 Poffices Agent。

    优先级（从高到低）：
      1. cli_agent_type（CLI 显式传入）
      2. config.extra["agent_type"]（实验配置文件）
      3. 默认值 "rule"（保守兜底）

    Args:
        config: 实验配置（ExperimentConfig），读取 extra.agent_type/agent_provider/agent_model
        cli_agent_type: CLI 覆盖的 agent_type，"llm" / "rule" / None（不覆盖）
        cli_provider: CLI 覆盖的 LLM 提供商
        cli_model: CLI 覆盖的 LLM 模型名称
        default_agent_name: 本 run 待测 Agent 名称（由 resolve_agent_under_test 得到）

    Returns:
        PofficesLLMAgent 或 PofficesAgent 实例，均满足 AgentProtocol。
    """
    extra = config.extra or {}

    agent_type = cli_agent_type or extra.get("agent_type") or "rule"
    provider = cli_provider or extra.get("agent_provider") or None
    model = cli_model or extra.get("agent_model") or None

    if agent_type == "llm":
        from raft.agents.poffices_llm_agent import PofficesLLMAgent

        block_catalog = resolve_block_catalog(config)
        available_agents = resolve_allowed_agents(config)
        agent = PofficesLLMAgent(
            provider=provider,
            model=model,
            fallback_on_error=True,
            block_catalog=block_catalog if block_catalog else None,
            available_agents=available_agents if available_agents else None,
            default_agent_name=default_agent_name,
        )
        logger.info(
            "[AgentFactory] 创建 PofficesLLMAgent，model=%s provider=%s 待测=%s",
            agent.model,
            provider or "qwen",
            default_agent_name,
        )
        return agent

    from raft.agents.poffices_agent import PofficesAgent

    logger.info("[AgentFactory] 创建 PofficesAgent（规则驱动）待测=%s", default_agent_name)
    return PofficesAgent(default_agent_name=default_agent_name)
