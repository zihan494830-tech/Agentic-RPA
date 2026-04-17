# B6: 决策组件（规则/LLM/多 Agent）；Poffices 场景下待测 Agent = 页面上的产品（Research Proposal 等）
from raft.agents.base import AgentProtocol
from raft.agents.mock_agent import MockAgent, MultiRoleMockAgent, Phase2MockAgent
from raft.agents.llm_agent import LLMAgent
from raft.agents.multi_agent import MultiAgentRegistry
from raft.agents.poffices_agent import PofficesAgent
from raft.agents.poffices_llm_agent import PofficesLLMAgent
from raft.agents.factory import create_poffices_agent

__all__ = [
    "AgentProtocol",
    "MockAgent",
    "MultiRoleMockAgent",
    "Phase2MockAgent",
    "LLMAgent",
    "MultiAgentRegistry",
    "PofficesAgent",
    "PofficesLLMAgent",
    "create_poffices_agent",
]
