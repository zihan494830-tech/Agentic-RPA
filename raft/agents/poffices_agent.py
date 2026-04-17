"""
B6 决策组件：Poffices 流程的规则驱动实现。
根据 state 与最近执行结果决定调用 poffices_bootstrap 或 poffices_query，以驱动对**待测 Agent**（Poffices 页面上的产品）的测试。
待测 Agent 由 default_agent_name 指定（来自配置/CLI），首次 bootstrap 时传入 options.agent_name。
"""
from raft.contracts.models import ToolCall


class PofficesAgent:
    """
    将 Poffices 登录 + 选 Agent + Query 流程作为 B6 决策逻辑接入。
    主逻辑：
    - 未 ready：调用 poffices_bootstrap（带 options.agent_name）
    - 已 ready：调用 poffices_query
    - 查询成功后停止；失败时按错误类型决定是否重试
    """

    def __init__(self, *, default_agent_name: str | None = None) -> None:
        self._default_agent_name = default_agent_name

    def _bootstrap_params(self) -> dict:
        if self._default_agent_name:
            return {"options": {"agent_name": self._default_agent_name}}
        return {}

    def run(
        self,
        agent_input_context: dict,
        task_description: str = "",
    ) -> list[ToolCall]:
        step = agent_input_context.get("current_step_index", 0)
        state = agent_input_context.get("state") or {}
        last_result = agent_input_context.get("last_execution_result") or {}

        if not state.get("poffices_ready"):
            return [ToolCall(tool_name="poffices_bootstrap", params=self._bootstrap_params())]

        query = state.get("query") or _query_from_description(task_description) or "Hello"
        if not isinstance(query, str) or not query.strip():
            return []
        query = query.strip()

        if isinstance(last_result, dict) and last_result:
            if last_result.get("tool_name") == "poffices_query" and last_result.get("success") is True:
                return []
            if last_result.get("tool_name") == "get_response" and last_result.get("success") is True:
                return []
            if last_result.get("success") is False:
                error_type = last_result.get("error_type")
                if error_type in {"timeout", "rpa_execution_failed", "element_not_found"}:
                    return [ToolCall(tool_name="poffices_query", params={"query": query})]
                return []

        if state.get("poffices_ready"):
            return [ToolCall(tool_name="poffices_query", params={"query": query})]

        if step == 0:
            return [ToolCall(tool_name="poffices_bootstrap", params=self._bootstrap_params())]
        if step == 1:
            return [ToolCall(tool_name="poffices_query", params={"query": query})]

        return []


def _query_from_description(desc: str) -> str | None:
    """从任务描述中尽量提取查询内容（简单启发）。"""
    if not desc or not isinstance(desc, str):
        return None
    # 例如 "在 Poffices 上查询：介绍一下你自己" -> "介绍一下你自己"
    for prefix in ("查询：", "query:", "问题：", "提问："):
        if prefix in desc:
            return desc.split(prefix, 1)[-1].strip()
    return None
