"""单次 run 内 LLM API 耗时采集：通过 ContextVar 汇总到 Orchestrator 的 llm_timing_events。"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_llm_sink: ContextVar[list[dict[str, Any]] | None] = ContextVar("raft_llm_timing_sink", default=None)


def attach_llm_timing_sink(sink: list[dict[str, Any]]) -> Any:
    return _llm_sink.set(sink)


def reset_llm_timing_sink(token: Any) -> None:
    _llm_sink.reset(token)


def record_llm_api_call(elapsed_ms: int, label: str | None = None) -> None:
    """记录一次 LLM chat 调用耗时（毫秒）；无活跃 sink 时忽略。"""
    sink = _llm_sink.get()
    if sink is None:
        return
    if elapsed_ms < 0:
        return
    sink.append({"label": label or "llm_chat", "elapsed_ms": int(elapsed_ms)})
