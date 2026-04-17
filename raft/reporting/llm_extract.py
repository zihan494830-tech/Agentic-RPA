"""
使用 LLM 从 Agent 原始输出中提取「正文」：去掉问候、任务说明、Time of preparation/completion、
Disclaimer 及之后内容，只保留文档标题与正文（多段时支持取第一段/最后一段）。

与判分、出题共用同一套 API：OPENAI_API_KEY、RAFT_LLM_PROVIDER（默认 qwen）等，无需单独配置。
环境变量 RAFT_LLM_EXTRACT_BODY=1 或 true 开启；未开启或调用失败时返回 None，由 output_scope 回退到规则提取。
"""
import os

from raft.core.llm_client import build_openai_client

# 送入 LLM 的原文最大字符数，避免超长导致超 token 或超时
MAX_RAW_CHARS = 14_000

_EXTRACT_SYSTEM = """You are a text extraction assistant. Your task is to extract ONLY the final document body from agent output.

The input may contain:
- Greetings (e.g. "Good evening, Toby Huang!")
- Task description, document parameters, "Time of preparation", "Time of completion"
- The actual document: title, sections (e.g. Research Background, Introduction), body, references
- Footer lines like "The presence of images..." or "Disclaimer: ..." and everything after

Rules:
1. Remove everything before the document title (greeting, task description, time lines).
2. Remove "Disclaimer" and everything after it; remove "The presence of images" and everything after it.
3. Keep only the document: its title, section headers, and body text. Keep references/links if they are part of the document.
4. Output in the same language as the document. Do not add any commentary or prefix.
5. If there are multiple document sections (e.g. separated by "Time of completion" or "Disclaimer"), {segment_instruction}
6. If there is no document body (only meta/status text), output nothing or a single line: NONE."""

_EXTRACT_USER_TEMPLATE = """Extract only the document body from the following text. Remove greetings, task description, "Time of preparation", "Time of completion", and "Disclaimer" and everything after.

Input:
---
{raw}
---

Output (document body only, or NONE if no document):"""


def extract_body_with_llm(
    raw: str,
    *,
    take_last: bool = True,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    max_chars: int = MAX_RAW_CHARS,
) -> str | None:
    """
    使用 LLM 从原始输出中提取正文。成功返回正文字符串，失败或无可提取时返回 None（调用方回退到规则）。

    take_last: True 时若有多个文档段则取最后一段，False 取第一段（与 output_scope 的 take_last 一致）。
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    client, model_name = build_openai_client(
        provider=(
            provider
            or os.environ.get("RAFT_LLM_EXTRACT_PROVIDER")
            or os.environ.get("RAFT_LLM_PROVIDER")
            or "qwen"
        ),
        model=model,
        api_key=api_key,
        base_url=base_url,
        openai_env_fallbacks=("RAFT_LLM_EXTRACT_MODEL", "RAFT_LLM_JUDGE_MODEL"),
        qwen_env_fallbacks=("RAFT_LLM_EXTRACT_MODEL",),
    )
    if client is None:
        return None

    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[... truncated ...]"

    segment_instruction = (
        "return only the LAST document section."
        if take_last
        else "return only the FIRST document section."
    )
    system_content = _EXTRACT_SYSTEM.format(segment_instruction=segment_instruction)
    user_content = _EXTRACT_USER_TEMPLATE.format(raw=text)

    try:
        resp = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            max_tokens=8000,
            temperature=0.1,
        )
        out = (resp.choices[0].message.content or "").strip()
        if not out or out.upper() == "NONE":
            return None
        return out
    except Exception:
        return None


def is_llm_extract_enabled() -> bool:
    """是否开启 LLM 正文提取（环境变量 RAFT_LLM_EXTRACT_BODY=1 或 true）。"""
    v = os.environ.get("RAFT_LLM_EXTRACT_BODY", "").strip().lower()
    return v in ("1", "true", "yes", "on")
