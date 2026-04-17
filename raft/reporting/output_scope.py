"""
规定：待测 Agent 的输出范围。

策略：绿色提示出现后已读取整页文本，此处只做「删掉无关、保留正文」：
- 可选 LLM 提取：当环境变量 RAFT_LLM_EXTRACT_BODY=1 或 true 时，优先用 LLM 提取正文，失败或未配置时回退到规则。
- Poffices 元信息块：若存在整行仅为「Time of completion: X.Xs」的行（严格整行匹配，避免正文中提及该短语时误删），则该行及之前全部删除；take_last 与分段一致（取第一处/最后一处），保证每轮输出完整。
- 开头：删到「It approximately takes X minute(s)」那一行为止（assignment 结束）。
- 结尾：删掉「The presence of images」/「Disclaimer」及其之后全部内容。
- 中间全部视为正文保留（含参考文献、链接等）。
多轮时按 Disclaimer 分段，每段做上述删除后取第一段或最后一段。
"""
import re

# 若上游误把整页 HTML 当正文（含 <script> 内 Elementor/WP 配置），先剥掉 script/style，再按 Poffices 规则取报告段
_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)


def _strip_html_script_style_blocks(text: str) -> str:
    if not text or ("<script" not in text.lower() and "<style" not in text.lower()):
        return text
    t = _SCRIPT_BLOCK_RE.sub("", text)
    t = _STYLE_BLOCK_RE.sub("", t)
    return t


def _try_llm_extract(raw: str, take_last: bool) -> str | None:
    """若启用 RAFT_LLM_EXTRACT_BODY 则调用 LLM 提取正文，否则或失败时返回 None。"""
    try:
        from raft.reporting.llm_extract import extract_body_with_llm, is_llm_extract_enabled
        if not is_llm_extract_enabled():
            return None
        return extract_body_with_llm(raw, take_last=take_last)
    except Exception:
        return None


# 系统尾：该行及之后全部删除
_TAIL_SYSTEM_RE = re.compile(
    r"^\s*(The presence of images|Disclaimer\s*:).*",
    re.IGNORECASE,
)

# 开头无关内容结束标记：含此内容的行及之前全部删除（任一种匹配即可）
_HEAD_JUNK_END_RE = re.compile(
    r"it approximately takes \d+ minute|preparing your document",
    re.IGNORECASE,
)

# Poffices 元信息块结束：仅匹配整行仅为「Time of completion: X.Xs」的短行，避免正文中出现该短语时误删
_POFFICES_INTRO_END_RE = re.compile(
    r"^\s*Time of completion\s*:\s*[\d.]*s?\s*$",
    re.IGNORECASE,
)


def _strip_poffices_intro(text: str, *, take_last: bool = True) -> str:
    """
    若文本中包含「Time of completion:」整行（仅该行，避免误删正文），则从该行之后开始保留。
    take_last=True 时取最后一处（最后一轮文档），take_last=False 时取第一处（第一轮文档），与 extract 的 take_last 一致。
    """
    if not text or not text.strip():
        return text
    lines = text.split("\n")
    indices = [i for i in range(len(lines)) if _POFFICES_INTRO_END_RE.match(lines[i].strip())]
    if not indices:
        return text
    i = indices[-1] if take_last else indices[0]
    rest = "\n".join(lines[i + 1 :]).strip()
    return rest if rest else text


def _find_report_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """
    按系统尾分段，每段内「删开头到 It approximately takes 那行、删结尾到 Disclaimer」，
    剩余区间即为一个报告块。
    """
    blocks: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        # 本段结束位置（系统尾或文末）
        end = len(lines)
        for j in range(i, len(lines)):
            if _TAIL_SYSTEM_RE.match(lines[j].strip()):
                end = j
                break
        # 本段 [i, end) 内：找最后一行含 "It approximately takes X minute" 的行号，正文从下一行开始
        start = i
        for k in range(i, end):
            if _HEAD_JUNK_END_RE.search(lines[k].strip()):
                start = k + 1
        if start < end:
            blocks.append((start, end))
        i = end + 1 if end < len(lines) else len(lines)
    return blocks


def strip_system_format_from_agent_output(raw: str) -> str:
    """删掉开头 assignment 与结尾系统尾，保留正文。取第一段。"""
    return extract_last_report_from_full_output(raw, take_last=False)


def extract_last_report_from_full_output(raw: str, *, take_last: bool = True) -> str:
    """
    整页文本已读入后，删掉无关、保留正文。
    - 若启用 RAFT_LLM_EXTRACT_BODY，优先用 LLM 提取正文；成功则直接返回，失败或未启用则用规则。
    - 规则：先去掉 Poffices 元信息块（到 Time of completion 那行为止），再按 Disclaimer 分段，每段删「到 It approximately takes 那行」和「Disclaimer 及之后」。
    - take_last=True 取最后一段（多轮最新一轮），take_last=False 取第一段。
    """
    if not raw or not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text:
        return ""
    text = _strip_html_script_style_blocks(text)

    llm_out = _try_llm_extract(text, take_last=take_last)
    if llm_out is not None and len(llm_out.strip()) > 0:
        return llm_out.strip()

    text = _strip_poffices_intro(text, take_last=take_last)
    if not text:
        return ""

    lines = text.split("\n")
    blocks = _find_report_blocks(lines)
    if not blocks:
        return text

    min_len = 50
    filtered = [(s, e) for s, e in blocks if len("\n".join(lines[s:e]).strip()) >= min_len]
    if not filtered:
        filtered = blocks
    else:
        # 当首/尾块过短（如 UI 残留 "New question"）时，不采纳，优先取有效块
        want_idx = -1 if take_last else 0
        want_block = blocks[want_idx]
        want_content = "\n".join(lines[want_block[0]:want_block[1]]).strip()
        if want_block not in filtered and len(want_content) >= min_len:
            if take_last:
                filtered = [b for b in filtered if b != want_block] + [want_block]
            else:
                filtered = [want_block] + [b for b in filtered if b != want_block]
        # 否则短块不采纳

    # 当 take_last 且最后块过短（< min_len）时，改用最长的块，避免误取 UI 残留（如 "New question"）
    if take_last and len(filtered) > 1:
        last_content = "\n".join(lines[filtered[-1][0]:filtered[-1][1]]).strip()
        if len(last_content) < min_len:
            longest = max(filtered, key=lambda b: len("\n".join(lines[b[0]:b[1]]).strip()))
            if len("\n".join(lines[longest[0]:longest[1]]).strip()) > len(last_content):
                filtered = [longest]

    start, end = filtered[-1] if take_last else filtered[0]
    return "\n".join(lines[start:end]).strip()
