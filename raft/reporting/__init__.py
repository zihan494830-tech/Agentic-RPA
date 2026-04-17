"""测试报告模块：接入 LLM 整理并输出多轮测试报告。"""
from raft.reporting.llm_report import build_report_with_llm
from raft.reporting.output_scope import (
    extract_last_report_from_full_output,
    strip_system_format_from_agent_output,
)

__all__ = [
    "build_report_with_llm",
    "extract_last_report_from_full_output",
    "strip_system_format_from_agent_output",
]
