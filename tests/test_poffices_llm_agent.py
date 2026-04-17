"""单元测试：PofficesLLMAgent 决策逻辑与 fallback 行为。

测试策略：
- 通过 unittest.mock.patch 拦截 OpenAI 客户端，避免真实 HTTP 调用
- 验证 LLM 正常返回时能正确解析并输出 tool_calls
- 验证 LLM 返回无效 JSON 时 fallback 到规则 Agent
- 验证 LLM 调用抛出异常时 fallback 到规则 Agent
- 验证 LLM 返回空 tool_calls（主动停止）不触发 fallback
- 验证 prompt 包含关键信息（state、task_description 等）
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from raft.agents.poffices_llm_agent import (
    PofficesLLMAgent,
    _build_decision_prompt,
    _parse_tool_calls,
)
from raft.contracts.models import ToolCall


# ---------- _parse_tool_calls 单元测试 ----------


class TestParseToolCalls:
    def test_valid_single_tool_call(self):
        text = '{"tool_calls": [{"tool_name": "poffices_bootstrap", "params": {}}]}'
        result = _parse_tool_calls(text)
        assert result is not None
        assert len(result) == 1
        assert result[0].tool_name == "poffices_bootstrap"

    def test_valid_query_with_params(self):
        text = '{"tool_calls": [{"tool_name": "poffices_query", "params": {"query": "你好"}}]}'
        result = _parse_tool_calls(text)
        assert result is not None
        assert result[0].tool_name == "poffices_query"
        assert result[0].params == {"query": "你好"}

    def test_empty_tool_calls_means_stop(self):
        text = '{"tool_calls": []}'
        result = _parse_tool_calls(text)
        assert result == []

    def test_llm_wraps_json_in_markdown(self):
        text = '```json\n{"tool_calls": [{"tool_name": "poffices_bootstrap", "params": {}}]}\n```'
        result = _parse_tool_calls(text)
        assert result is not None
        assert result[0].tool_name == "poffices_bootstrap"

    def test_invalid_json_returns_none(self):
        assert _parse_tool_calls("not json at all") is None

    def test_missing_tool_calls_key_returns_none(self):
        assert _parse_tool_calls('{"action": "bootstrap"}') is None

    def test_tool_calls_not_list_returns_none(self):
        assert _parse_tool_calls('{"tool_calls": "bootstrap"}') is None


# ---------- _build_decision_prompt 单元测试 ----------


class TestBuildDecisionPrompt:
    def test_contains_task_description(self):
        ctx = {"current_step_index": 0, "state": {}}
        prompt = _build_decision_prompt(ctx, "测试 Poffices 能力")
        assert "测试 Poffices 能力" in prompt

    def test_contains_state(self):
        ctx = {"current_step_index": 1, "state": {"poffices_ready": True}}
        prompt = _build_decision_prompt(ctx, "")
        assert "poffices_ready" in prompt

    def test_contains_last_result_when_present(self):
        ctx = {
            "current_step_index": 2,
            "state": {"poffices_ready": True},
            "last_execution_result": {"success": False, "error_type": "timeout"},
        }
        prompt = _build_decision_prompt(ctx, "")
        assert "timeout" in prompt

    def test_no_last_result_section_when_absent(self):
        ctx = {"current_step_index": 0, "state": {}}
        prompt = _build_decision_prompt(ctx, "")
        assert "上一步执行结果" not in prompt

    def test_default_task_description(self):
        ctx = {"current_step_index": 0, "state": {}}
        prompt = _build_decision_prompt(ctx, "")
        assert "Poffices" in prompt


# ---------- PofficesLLMAgent 集成测试（mock LLM） ----------


def _make_llm_response(tool_calls_json: list) -> MagicMock:
    """构造 OpenAI SDK 风格的响应 mock。"""
    content = json.dumps({"tool_calls": tool_calls_json}, ensure_ascii=False)
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    response = SimpleNamespace(choices=[choice])
    return response


class TestPofficesLLMAgentWithMock:
    """通过 patch chat_completion_with_retry 测试 PofficesLLMAgent，无需真实 API。"""

    def setup_method(self):
        self._patchers: list = []

    def teardown_method(self):
        for p in self._patchers:
            p.stop()
        self._patchers = []

    def _make_agent(self, llm_response=None, side_effect=None, fallback_on_error=True):
        """创建 agent 并 patch chat_completion_with_retry。"""
        agent = PofficesLLMAgent(api_key="test-key", fallback_on_error=fallback_on_error)
        mock_fn = MagicMock()
        if side_effect:
            mock_fn.side_effect = side_effect
        elif llm_response is not None:
            mock_fn.return_value = llm_response
        patcher = patch(
            "raft.agents.poffices_llm_agent.chat_completion_with_retry", mock_fn
        )
        patcher.start()
        self._patchers.append(patcher)
        agent._mock_llm = mock_fn
        return agent

    def test_llm_decides_bootstrap_when_not_ready(self):
        resp = _make_llm_response([{"tool_name": "poffices_bootstrap", "params": {}}])
        agent = self._make_agent(llm_response=resp)
        ctx = {"current_step_index": 0, "state": {"poffices_ready": False}}
        calls = agent.run(ctx, "测试")
        assert len(calls) == 1
        assert calls[0].tool_name == "poffices_bootstrap"

    def test_llm_decides_query_when_ready(self):
        resp = _make_llm_response(
            [{"tool_name": "poffices_query", "params": {"query": "介绍你自己"}}]
        )
        agent = self._make_agent(llm_response=resp)
        ctx = {
            "current_step_index": 1,
            "state": {"poffices_ready": True, "query": "介绍你自己"},
        }
        calls = agent.run(ctx, "")
        assert calls[0].tool_name == "poffices_query"
        assert calls[0].params["query"] == "介绍你自己"

    def test_llm_stops_after_success(self):
        resp = _make_llm_response([])
        agent = self._make_agent(llm_response=resp)
        ctx = {
            "current_step_index": 2,
            "state": {"poffices_ready": True},
            "last_execution_result": {"tool_name": "poffices_query", "success": True},
        }
        calls = agent.run(ctx, "")
        assert calls == []

    def test_fallback_on_invalid_json(self):
        """LLM 返回无法解析的内容时，fallback 到规则 Agent（初始状态 → bootstrap）。"""
        message = SimpleNamespace(content="我不知道该怎么做")
        choice = SimpleNamespace(message=message)
        mock_resp = SimpleNamespace(choices=[choice])
        agent = self._make_agent(llm_response=mock_resp)
        ctx = {"current_step_index": 0, "state": {}}
        calls = agent.run(ctx, "")
        assert len(calls) == 1
        assert calls[0].tool_name == "poffices_bootstrap"

    def test_fallback_on_api_exception(self):
        """LLM API 抛出异常时，fallback 到规则 Agent。"""
        agent = self._make_agent(side_effect=RuntimeError("network error"))
        ctx = {"current_step_index": 0, "state": {}}
        calls = agent.run(ctx, "")
        assert len(calls) == 1
        assert calls[0].tool_name == "poffices_bootstrap"

    def test_no_fallback_when_disabled(self):
        """fallback_on_error=False 时，LLM 失败直接返回空列表而非规则决策。"""
        agent = self._make_agent(
            side_effect=RuntimeError("fail"), fallback_on_error=False
        )
        ctx = {"current_step_index": 0, "state": {}}
        calls = agent.run(ctx, "")
        assert calls == []

    def test_llm_call_includes_system_and_user_messages(self):
        resp = _make_llm_response([{"tool_name": "poffices_bootstrap", "params": {}}])
        agent = self._make_agent(llm_response=resp)
        ctx = {"current_step_index": 0, "state": {}}
        agent.run(ctx, "测试任务")

        call_kwargs = agent._mock_llm.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs.args[1]
        roles = [m["role"] for m in messages]
        assert "system" in roles
        assert "user" in roles

    def test_system_prompt_contains_available_blocks(self):
        from raft.agents.poffices_llm_agent import _LEGACY_SYSTEM_PROMPT

        assert "poffices_bootstrap" in _LEGACY_SYSTEM_PROMPT
        assert "poffices_query" in _LEGACY_SYSTEM_PROMPT
        # 无 block_catalog 时 agent 使用 legacy prompt
        agent = self._make_agent()
        prompt = agent._build_system_prompt()
        assert "poffices_bootstrap" in prompt
        assert "poffices_query" in prompt

    def test_system_prompt_contains_default_agent_name(self):
        agent = PofficesLLMAgent(
            api_key="test-key",
            block_catalog=[{"block_id": "app_ready", "description": "test", "params": {}}],
            default_agent_name="Market Analysis",
        )
        prompt = agent._build_system_prompt()
        assert "Market Analysis" in prompt
        assert "本 run 待测 Agent" in prompt

    def test_retry_on_timeout_error(self):
        """上一步 timeout，LLM 决定重试 poffices_query。"""
        resp = _make_llm_response(
            [{"tool_name": "poffices_query", "params": {"query": "Hello"}}]
        )
        agent = self._make_agent(llm_response=resp)
        ctx = {
            "current_step_index": 2,
            "state": {"poffices_ready": True, "query": "Hello"},
            "last_execution_result": {
                "tool_name": "poffices_query",
                "success": False,
                "error_type": "timeout",
            },
        }
        calls = agent.run(ctx, "")
        assert calls[0].tool_name == "poffices_query"
