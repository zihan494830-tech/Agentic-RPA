"""Poffices Custom Block 专用路由（B3.5 规划入口）。

**两条用途（请区分）：**

1. **本机 / 默认 HTTP 行为** `planning_profile=rpa_local_default`（**字段默认值**）：与本地 RPA 能力一致——未传 `block_catalog` 时仍用
   **app_ready / send_query / get_response**，**不影响** `run_poffices_agent` / Orchestrator（它们**不经过**本路由，直接调 `build_goal_plan`）。

2. **Poffices 画布编排** `planning_profile=canvas`：在发布到 Poffices 的请求体里**显式**写上该值，并传 **非空 block_catalog**，
   规划器只在你列出的块里编排，**跳出 RPA 隐含默认**；见 `docs/poffices_agent_import.json` 示例。
   成功时 `data.selected_agents` 会从各步 `params` 通用抽取 Agent 顺序（`agent_name` / `agent` / `options.*` / `agents[]`）；
   `data.agents_planned` 仍仅来自 **app_ready**，供本机 RPA 兼容。

兼容：部分 Poffices API 会以 OpenAI Chat 形态 POST（含 messages），会先归一化再校验；成功响应可包成 `chat.completion` 供画布读 `content`。

轨迹：logs/poffices_api/trace.jsonl；/run 本地 RPA 轨迹目录 logs/poffices_api_run（单文件覆盖，见 _execute_run_sync）。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs" / "poffices_api"
_MAX_LOG_CHARS = 48_000

from raft.core.planner import build_goal_plan, linearize_goal_plan, parse_goal

router = APIRouter(prefix="/api/v1/poffices", tags=["Poffices Custom Block"])


# ── 请求 / 响应模型 ─────────────────────────────────────────────────────────

def _catalog_has_usable_block_id(catalog: list[dict[str, Any]] | None) -> bool:
    if not catalog:
        return False
    return any(
        isinstance(x, dict) and isinstance(x.get("block_id"), str) and x["block_id"].strip()
        for x in catalog
    )


class PofficesPlanRequest(BaseModel):
    """Poffices Custom Block：goal +（画布模式）block_catalog → 规划「调用哪些块、何种顺序/参数」。"""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="请求唯一标识")
    planning_profile: Literal["canvas", "rpa_local_default"] = Field(
        default="rpa_local_default",
        description=(
            "默认 rpa_local_default：与本机 RPA 一致，未传 block_catalog 时用 app_ready/send_query/get_response。"
            "Poffices 上跳出 RPA 时请设为 canvas 并传入非空 block_catalog。"
        ),
    )
    goal: str = Field(..., description="自然语言目标；规划器据此理解意图并编排 block_catalog 中的块")
    block_catalog: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "画布编排模式下必填（至少一项含非空 block_id）：与 Poffices 工作流中可调用的块 id/说明一致。"
            "rpa_local_default 且留空时使用本机默认 RPA 三块。"
        ),
    )
    context: dict[str, Any] | None = Field(
        default=None,
        description=(
            "上游 block 的上下文，可包含：\n"
            "  agent_name   - 要测试的 Poffices Agent 名称\n"
            "  agents_to_test - 多 Agent 列表（JSON 数组，或画布展开后的 JSON 数组字符串）\n"
            "  query        - 若已由上游 block 生成，直接透传给规划器\n"
            "  db_search_result   - 上游 DB Search block 的输出（{layer_name_db-search_output}）\n"
            "  web_crawler_result - 上游 Web Crawler block 的输出（{layer_name_web-crawler_output}）\n"
            "  previous_rounds    - 多轮时的历史"
        ),
    )
    use_llm_planner: bool = Field(
        default=True,
        description="是否启用 LLM 规划（False 则走规则兜底，速度更快但灵活度低）",
    )
    llm_provider: str | None = Field(default=None, description="LLM 提供商，如 qwen / openai")
    llm_model: str | None = Field(default=None, description="LLM 模型名称，不传则使用默认")
    api_version: str = Field(default="v1", description="契约版本")

    @model_validator(mode="after")
    def _canvas_requires_catalog(self) -> PofficesPlanRequest:
        if self.planning_profile == "canvas" and not _catalog_has_usable_block_id(self.block_catalog):
            raise ValueError(
                "planning_profile=canvas 时必须提供非空 block_catalog（每项含 block_id）。"
                "本接口在画布上的用途是编排你声明的块以达成 goal，而非隐式默认 RPA；"
                "本机 RPA 烟测请设 planning_profile=rpa_local_default。"
            )
        return self


class PlannedStep(BaseModel):
    """单个规划步骤，对应一次 block 调用。"""
    step_id: str
    tool_name: str = Field(description="block_id，即要在 Poffices 画布上调用的块")
    params: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None


class PofficesPlanResponse(BaseModel):
    """Poffices Custom Block 规划响应。"""
    request_id: str
    api_version: str = "v1"
    code: str = Field(description="ok 或错误码")
    data: PofficesPlanData | None = None
    error: PofficesError | None = None


class PofficesPlanData(BaseModel):
    """规划成功：在 block_catalog 允许范围内产出的块调用序列（画布或 RPA 均由 catalog 决定）。"""
    planned_steps: list[PlannedStep] = Field(description="有序步骤列表，按顺序执行即可")
    plan_source: str = Field(description="规划来源：rule_fallback / llm / compound_block")
    goal_intent_summary: str | None = Field(
        default=None,
        description="GoalParser 对 goal 的简要解读（可用于画布 LLM 层的上下文）",
    )
    step_count: int
    agents_planned: list[str] = Field(
        default_factory=list,
        description=(
            "仅从 app_ready 步骤的 params.options.agent_name 抽取（与本机 RPA 契约一致，向后兼容）。"
            "画布无 app_ready 时请读 selected_agents。"
        ),
    )
    selected_agents: list[str] = Field(
        default_factory=list,
        description=(
            "通用 Agent 顺序列表：按 planned_steps 顺序从各步 params 抽取 "
            "（agent_name / agent / options.* / agents[]），去重保留首次出现。"
            "含 app_ready 时的选项；画布块名可为 invoke_agent、agent_over_agent 等任意 block_id。"
        ),
    )


class PofficesError(BaseModel):
    """错误信息。"""
    code: str
    message: str
    details: dict[str, Any] | None = None


def _truncate(s: str, n: int = _MAX_LOG_CHARS) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"...(truncated,{len(s)}chars)"


def _append_trace(record: dict[str, Any]) -> None:
    """追加一行 JSON 到 logs/poffices_api/trace.jsonl，便于分析 Poffices 调用。"""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = _LOG_DIR / "trace.jsonl"
        record.setdefault("ts", datetime.now(timezone.utc).isoformat())
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("poffices trace log failed: %s", e)


def _extract_goal_from_openai_messages(messages: list[Any]) -> str | None:
    """从 OpenAI 风格 messages 中取最后一条 user 文本。"""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            return c.strip()
        if isinstance(c, list):
            parts: list[str] = []
            for item in c:
                if isinstance(item, dict) and item.get("type") == "text":
                    t = item.get("text")
                    if isinstance(t, str) and t.strip():
                        parts.append(t.strip())
                elif isinstance(item, str) and item.strip():
                    parts.append(item.strip())
            if parts:
                return "\n".join(parts)
    return None


def _normalize_body_for_plan(raw: dict[str, Any]) -> dict[str, Any]:
    """
    将 Poffices / openllm 可能发来的 body 转为 PofficesPlanRequest 可校验形态。
    - 已有非空 goal：原样返回（多余键 Pydantic 默认忽略）
    - 仅含 messages（OpenAI chat）：从最后一条 user 抽出 goal
    - 仅含 input / prompt 字符串：映射为 goal
    - context 若为 JSON 字符串：解析为对象
    """
    out = dict(raw)
    if out.get("goal") is not None and not isinstance(out["goal"], str):
        out["goal"] = str(out["goal"])
    ctx = out.get("context")
    if isinstance(ctx, str) and ctx.strip():
        try:
            parsed = json.loads(ctx)
            if isinstance(parsed, dict):
                out["context"] = parsed
        except json.JSONDecodeError:
            pass
    if isinstance(out.get("goal"), str) and out["goal"].strip():
        return out
    messages = out.get("messages")
    if isinstance(messages, list) and messages:
        g = _extract_goal_from_openai_messages(messages)
        if g:
            g_strip = g.strip()
            # user.content 常为整段规划 JSON（含 goal/context/block_catalog），需展开而非整段当 goal
            if g_strip.startswith("{") and '"goal"' in g_strip:
                try:
                    inner = json.loads(g_strip)
                    if isinstance(inner, dict) and isinstance(inner.get("goal"), str) and inner["goal"].strip():
                        for k, v in inner.items():
                            if k == "messages":
                                continue
                            out[k] = v
                        return out
                except json.JSONDecodeError:
                    pass
            out["goal"] = g
            return out
    if isinstance(out.get("input"), str) and out["input"].strip():
        out["goal"] = out["input"].strip()
        return out
    if isinstance(out.get("prompt"), str) and out["prompt"].strip():
        out["goal"] = out["prompt"].strip()
        return out
    return out


def _coerce_bools(d: dict[str, Any]) -> dict[str, Any]:
    """宽松处理 use_llm_planner 等字符串布尔。"""
    u = d.get("use_llm_planner")
    if isinstance(u, str):
        d["use_llm_planner"] = u.strip().lower() in ("1", "true", "yes", "on")
    return d


def _coerce_context_agents_to_test(ctx: dict[str, Any] | None) -> dict[str, Any]:
    """将 context.agents_to_test 规范为 list[str]，并清洗关键标量字段的 null/非字符串值。

    画布 Custom Block 模板里常写成带引号的占位符，展开后为 JSON 数组**字符串**（如 ``'[\"A\",\"B\"]'``），
    需 json.loads 后供规划器使用；若已是 list 则清洗为字符串列表。
    非 JSON 的单个 Agent 名也可整段作为单元素列表。

    同时对 agent_name / query 等规划器依赖的标量字段做防御性清洗：
    - null / 空字符串 → 移除（规划器收到 None 与字段不存在语义相同，但传入 null 更易引发 KeyError）
    - 非字符串 → 转换为字符串后清洗
    """
    if not ctx:
        return {}
    out = dict(ctx)

    # ── agents_to_test ──────────────────────────────────────────────────────
    raw = out.get("agents_to_test")
    if raw is not None:
        if isinstance(raw, list):
            out["agents_to_test"] = [str(x).strip() for x in raw if str(x).strip()]
        elif isinstance(raw, str):
            s = raw.strip()
            if not s:
                del out["agents_to_test"]
            else:
                try:
                    parsed = json.loads(s)
                except json.JSONDecodeError:
                    out["agents_to_test"] = [s]
                else:
                    if isinstance(parsed, list):
                        out["agents_to_test"] = [str(x).strip() for x in parsed if str(x).strip()]
                    elif isinstance(parsed, str) and parsed.strip():
                        out["agents_to_test"] = [parsed.strip()]
                    else:
                        del out["agents_to_test"]
        else:
            # 非 list / str（如 int / None 被误传）→ 移除
            del out["agents_to_test"]

    # ── 关键标量字段：null 或空字符串一律移除，避免规划器接收到无意义值 ──────
    for _key in ("agent_name", "query", "db_search_result", "web_crawler_result"):
        if _key not in out:
            continue
        val = out[_key]
        if val is None:
            del out[_key]
        elif not isinstance(val, str):
            coerced = str(val).strip()
            if coerced:
                out[_key] = coerced
            else:
                del out[_key]
        elif not val.strip():
            del out[_key]

    return out


def _wants_openai_chat_completion(raw: dict[str, Any]) -> bool:
    """Poffices 画布通常只认 choices[0].message.content。

    - 含 **messages**（OpenAI Chat 形态，且至少一条 user 消息）→ 必须包一层。
      仅含 system 消息不认定为 Chat 形态：无法提取 goal，且调用方大概率不期望 chat.completion 响应。
    - 仅含 **goal**、无 messages（部分平台把模板展开成直连 JSON，不再套 messages）→ 也必须包一层，否则仍显示 empty。
    - 显式 **response_envelope: \"native\"** 时保持原样 PofficesPlanResponse，供 Postman/脚本调试。
    """
    if raw.get("response_envelope") == "native":
        return False
    messages = raw.get("messages")
    if isinstance(messages, list) and any(
        isinstance(m, dict) and m.get("role") == "user" for m in messages
    ):
        return True
    g = raw.get("goal")
    if isinstance(g, str) and g.strip():
        return True
    return False


def _openai_chat_completion_dict(
    *,
    content: str,
    model: str | None = None,
    extracted_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """最小 chat.completion 信封，供画布把 assistant.content 当作本块输出。

    **extracted_data**（可选）：与 `support/pipeline` 流式响应中同名字段对齐——顶层结构化结果，
    便于 Poffices 在未可靠解析 `message.content` 字符串时仍取到 `agent_results` 等，减少空输出与重试。
    """
    out: dict[str, Any] = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": (model if isinstance(model, str) and model.strip() else None)
            or os.environ.get("RAFT_LLM_MODEL")
            or os.environ.get("QWEN_MODEL")
            or "deepseek-v3",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    if extracted_data is not None:
        out["extracted_data"] = extracted_data
    return out


def _sse_chat_completion_stream(
    *,
    content: str,
    model: str | None = None,
    extracted_data: dict[str, Any] | None = None,
):
    """SSE 流式响应生成器：返回 OpenAI chat.completion.chunk 格式。

    Poffices 的 Custom Block LLM 客户端默认以 stream 模式调用，期望 SSE 事件流
    （data: {...}\\n\\n，最后 data: [DONE]）。若返回普通 JSON，客户端会报
    "Failed to fetch stream: Bad Request" 并重试，导致 RPA 被反复触发。
    """
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model_name = (
        (model if isinstance(model, str) and model.strip() else None)
        or os.environ.get("RAFT_LLM_MODEL")
        or os.environ.get("QWEN_MODEL")
        or "deepseek-v3"
    )

    def _chunk(delta: dict[str, Any], finish_reason: str | None, include_extracted: bool = False) -> str:
        payload: dict[str, Any] = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }
        if include_extracted and extracted_data is not None:
            payload["extracted_data"] = extracted_data
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # 1. role 块 → 2. content 块 → 3. finish 块 → 4. [DONE]
    yield _chunk({"role": "assistant"}, None)
    yield _chunk({"content": content}, None)
    yield _chunk({}, "stop", include_extracted=True)
    yield "data: [DONE]\n\n"


def _sse_chunk_payload(
    *,
    chat_id: str,
    created: int,
    model_name: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    extracted_data: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if extracted_data is not None:
        payload["extracted_data"] = extracted_data
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _sse_run_with_heartbeat(
    req: "PofficesRunRequest",
    *,
    model: str | None,
    heartbeat_sec: float = 5.0,
    body_chunk_size: int = 2000,
):
    """边跑 RPA 边推 SSE 真 content delta（而不是 SSE 注释）。

    关键点（对齐已验证可用的参考实现）：
    - 心跳用 ``content="."`` 真 delta，而不是 ``: keepalive\\n\\n`` 注释：
      Poffices 客户端会把后者当作"流无进展"而判定异常并重试。
    - 最终结果按 ``body_chunk_size`` 字符分段推送，避免一次性大 chunk 被中间代理截断/缓冲。
    - chunk payload 只保留 ``id/choices[delta,finish_reason]``，去掉 ``object/created/model`` 等
      Poffices 可能不处理的字段，减小匹配面。
    """
    import asyncio

    resp_id = f"chatcmpl-{int(time.time())}"

    def _make_chunk(
        content: str | None = None,
        finish: bool = False,
        extracted: dict[str, Any] | None = None,
    ) -> str:
        delta = {"content": content} if content is not None else {}
        payload: dict[str, Any] = {
            "id": resp_id,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": "stop" if finish else None,
                }
            ],
        }
        if extracted is not None:
            payload["extracted_data"] = extracted
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # 立即推首段可见内容，让 Poffices 认定流已开始产出
    yield _make_chunk(content="🤖 RPA 测试启动...\n")

    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(None, _execute_run_sync, req)

    # 边跑边推 "." 维持流进展
    while True:
        try:
            resp = await asyncio.wait_for(asyncio.shield(task), timeout=heartbeat_sec)
            break
        except asyncio.TimeoutError:
            yield _make_chunk(content=".")

    resp_dict = resp.model_dump()
    body = json.dumps(resp_dict, ensure_ascii=False)

    yield _make_chunk(content="\n\n")
    for i in range(0, len(body), body_chunk_size):
        yield _make_chunk(content=body[i : i + body_chunk_size])

    yield _make_chunk(content="", finish=True, extracted=resp_dict)
    yield "data: [DONE]\n\n"


# ── 路由处理 ────────────────────────────────────────────────────────────────

_DEFAULT_BLOCK_CATALOG: list[dict[str, Any]] = [
    {"block_id": "app_ready",   "description": "进入应用并选择 Agent",              "params": {"options": "可选，含 agent_name"}},
    {"block_id": "send_query",  "description": "发送查询",                           "params": {"query": "必填"}},
    {"block_id": "get_response","description": "等待并取回结果",                     "params": {}},
]


def _extract_agents_planned(planned_calls: list) -> list[str]:
    """从 planned_calls 提取涉及的 agent 名称（仅 app_ready.options.agent_name，与历史行为一致）。"""
    agents: list[str] = []
    for tc in planned_calls:
        if tc.tool_name == "app_ready":
            opts = (tc.params or {}).get("options")
            if isinstance(opts, dict):
                name = opts.get("agent_name")
                if isinstance(name, str) and name.strip() and name not in agents:
                    agents.append(name.strip())
    return agents


def _extract_selected_agents_from_planned_calls(planned_calls: list) -> list[str]:
    """路线二：不依赖 RPA 块名，从各步 params 通用抽取 Agent（有序去重）。

    识别字段（单步内按此顺序追加，避免同一步重复）：
    - params.agent_name、params.agent
    - params.options 为 dict 时其中的 agent_name、agent
    - params.agents 为 list 时其中的字符串项（多 Agent 编排）
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(name: object) -> None:
        if not isinstance(name, str):
            return
        n = name.strip()
        if not n or n in seen:
            return
        seen.add(n)
        ordered.append(n)

    def _from_params(params: dict[str, Any]) -> None:
        _add(params.get("agent_name"))
        _add(params.get("agent"))
        opts = params.get("options")
        if isinstance(opts, dict):
            _add(opts.get("agent_name"))
            _add(opts.get("agent"))
        agents_list = params.get("agents")
        if isinstance(agents_list, list):
            for item in agents_list:
                _add(item)

    for tc in planned_calls:
        raw = getattr(tc, "params", None)
        params = raw if isinstance(raw, dict) else {}
        _from_params(params)

    return ordered


def _summarize_goal_intent(goal_intent: Any) -> str | None:
    """把 GoalIntent 转成一段可读摘要，供画布 LLM 层参考。"""
    if goal_intent is None:
        return None
    parts: list[str] = []
    if getattr(goal_intent, "content_intent", None):
        parts.append("内容意图：" + "；".join(goal_intent.content_intent))
    if getattr(goal_intent, "execution_constraints", None):
        parts.append("执行限制：" + "；".join(goal_intent.execution_constraints))
    if getattr(goal_intent, "quality_requirements", None):
        parts.append("质量要求：" + "；".join(goal_intent.quality_requirements))
    return "  ".join(parts) if parts else None


def _execute_plan(request: PofficesPlanRequest) -> PofficesPlanResponse:
    """执行规划逻辑（供 /plan 与测试复用）。"""
    try:
        if request.planning_profile == "rpa_local_default":
            block_catalog = request.block_catalog or _DEFAULT_BLOCK_CATALOG
        else:
            block_catalog = list(request.block_catalog or [])

        # 组装 initial_state：合并 context 中与规划相关的字段
        ctx = _coerce_context_agents_to_test(request.context)
        initial_state: dict[str, Any] = {}

        # 透传 agent 信息
        if ctx.get("agent_name"):
            initial_state["agent_name"] = ctx["agent_name"]
        if ctx.get("agents_to_test"):
            initial_state["agents_to_test"] = ctx["agents_to_test"]

        # 若上游已生成 query，直接用；否则让规划器从 goal 里理解
        if ctx.get("query"):
            initial_state["query"] = ctx["query"]
        else:
            # 以 goal 本身作为 task description 驱动规划器生成合理 query
            initial_state["query"] = request.goal

        # 把上游检索结果放入 state，规划器可在 LLM 模式下参考这些信息
        if ctx.get("db_search_result"):
            initial_state["db_search_result"] = ctx["db_search_result"]
        if ctx.get("web_crawler_result"):
            initial_state["web_crawler_result"] = ctx["web_crawler_result"]

        # 1. 解析 goal → GoalIntent（理解「要干什么」）
        goal_intent = parse_goal(
            request.goal,
            provider=request.llm_provider,
            model=request.llm_model,
        )

        # 2. 生成规划（GoalPlan）；复用已解析的 goal_intent，避免重复调用 LLM
        plan = build_goal_plan(
            block_catalog=block_catalog,
            initial_state=initial_state,
            task_description=request.goal,
            use_llm_planner=request.use_llm_planner,
            goal=request.goal,
            llm_provider=request.llm_provider,
            llm_model=request.llm_model,
            intent_override=goal_intent,
        )

        # 3. 线性化为有序步骤列表
        planned_calls = linearize_goal_plan(plan)

        planned_steps = [
            PlannedStep(
                step_id=getattr(tc, "step_id", f"s{i}") or f"s{i}",
                tool_name=tc.tool_name,
                params=tc.params or {},
                note=None,
            )
            for i, tc in enumerate(planned_calls)
        ]

        agents_legacy = _extract_agents_planned(planned_calls)
        selected = _extract_selected_agents_from_planned_calls(planned_calls)

        resp = PofficesPlanResponse(
            request_id=request.request_id,
            code="ok",
            data=PofficesPlanData(
                planned_steps=planned_steps,
                plan_source=plan.source,
                goal_intent_summary=_summarize_goal_intent(goal_intent),
                step_count=len(planned_steps),
                agents_planned=agents_legacy,
                selected_agents=selected,
            ),
        )
        _append_trace(
            {
                "event": "plan_ok",
                "request_id": request.request_id,
                "step_count": len(planned_steps),
                "plan_source": plan.source,
            }
        )
        return resp

    except Exception as exc:
        _append_trace(
            {
                "event": "plan_exception",
                "request_id": request.request_id,
                "error": str(exc),
                "goal": request.goal[:500] if request.goal else "",
            }
        )
        return PofficesPlanResponse(
            request_id=request.request_id,
            code="plan_failed",
            data=None,
            error=PofficesError(
                code="plan_failed",
                message=str(exc),
                details={"goal": request.goal},
            ),
        )


@router.post(
    "/plan",
    summary="Poffices Custom Block — 规划步骤",
    response_model=None,
)
async def poffices_plan(request: Request) -> PofficesPlanResponse | JSONResponse:
    """
    核心规划接口，供 Poffices Custom Block（Toby-RPA-Test）调用。

    支持两种常见请求体：
    1) 显式 JSON：{\"goal\": \"...\", \"context\": {...}, ...}（与 PofficesPlanRequest 一致）
    2) OpenAI Chat 兼容：{\"messages\": [{\"role\":\"user\",\"content\":\"...\"}], ...}（从 user 消息提取 goal）

    默认将响应包成 **chat.completion**（**choices[0].message.content** 为完整规划 JSON），兼容：
    OpenAI **messages** 形态、以及仅含 **goal** 的直连 JSON（无 messages）。画布依赖 content 才能显示块输出。

    调试原生顶层结构可传 **response_envelope: \"native\"**。

    画布上可把 DB Search / Web 的输出通过 context 字段传入；验收在画布侧完成。
    """
    raw_bytes = await request.body()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        _append_trace({"event": "decode_error", "error": str(e)})
        raise HTTPException(status_code=400, detail="Invalid UTF-8 body") from e
    try:
        raw = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        _append_trace({"event": "json_parse_error", "error": str(e), "body_preview": _truncate(text)})
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e

    if not isinstance(raw, dict):
        _append_trace({"event": "invalid_root", "type": type(raw).__name__})
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    _plan_hdrs = {
        k: request.headers.get(k)
        for k in (
            "user-agent", "x-forwarded-for", "x-real-ip", "origin", "referer",
            "x-request-id", "x-correlation-id", "x-poffices-agent-id",
            "x-poffices-run-id", "x-poffices-node-id", "host",
            "cf-connecting-ip", "forwarded",
        )
        if request.headers.get(k)
    }
    _append_trace({
        "event": "request_in",
        "client": getattr(request.client, "host", None),
        "headers": _plan_hdrs,
        "raw_preview": _truncate(json.dumps(raw, ensure_ascii=False)),
    })

    normalized = _coerce_bools(_normalize_body_for_plan(raw))
    openai_compat = _wants_openai_chat_completion(raw)
    try:
        req = PofficesPlanRequest.model_validate(normalized)
    except ValidationError as e:
        _append_trace(
            {
                "event": "validation_error",
                "pydantic_errors": e.errors(),
                "normalized_keys": list(normalized.keys()),
            }
        )
        if openai_compat:
            err_payload = json.dumps(
                {"code": "validation_error", "detail": e.errors()},
                ensure_ascii=False,
            )
            return JSONResponse(
                _openai_chat_completion_dict(
                    content=err_payload,
                    model=raw.get("model") if isinstance(raw.get("model"), str) else None,
                )
            )
        raise HTTPException(status_code=422, detail=e.errors()) from e

    resp = _execute_plan(req)
    if openai_compat:
        resp_dict = resp.model_dump()
        body = json.dumps(resp_dict, ensure_ascii=False)
        model_name = raw.get("model") if isinstance(raw.get("model"), str) else None
        if bool(raw.get("stream")):
            _append_trace({"event": "response_sse_stream", "request_id": req.request_id})
            return StreamingResponse(
                _sse_chat_completion_stream(
                    content=body,
                    model=model_name,
                    extracted_data=resp_dict,
                ),
                media_type="text/event-stream",
            )
        _append_trace({"event": "response_openai_chat_completion", "request_id": req.request_id})
        return JSONResponse(
            _openai_chat_completion_dict(
                content=body,
                model=model_name,
                extracted_data=resp_dict,
            )
        )
    return resp


@router.get("/health", summary="Poffices Block 健康检查")
def poffices_health() -> dict:
    """Poffices Custom Block 心跳检测，在 API Management 里可用此地址验证连通性。"""
    return {"status": "ok", "block": "Toby-RPA-Planner", "api_version": "v1"}


# ── /run：完整执行（规划 + RPA + 返回测试结果） ──────────────────────────────


class PofficesRunRequest(BaseModel):
    """Poffices 完整执行请求：goal + 可选 agents/query → 规划 + RPA + 结构化结果。"""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    goal: str = Field(..., description="自然语言测试目标，如「测试 Research Proposal agent 的市场分析能力」")
    agents_to_test: list[str] | None = Field(
        default=None,
        description="指定被测 Poffices Agent 名称列表；不传则由 goal 自动解析",
    )
    query: str | None = Field(
        default=None,
        description="发给 Agent 的问题；不传则由 LLM 根据 goal 自动生成",
    )
    llm_provider: str | None = Field(default=None, description="LLM 提供商，如 qwen / openai")
    llm_model: str | None = Field(default=None, description="LLM 模型，不传则使用默认")
    response_envelope: str | None = Field(
        default=None,
        description="调试用：传 native 可跳过 chat.completion 包装，直接返回原始响应结构",
    )


class AgentTestResult(BaseModel):
    """单个 Agent 测试结果。"""
    agent_name: str
    query: str
    response: str | None = Field(default=None, description="Agent 实际返回内容")
    success: bool


class PofficesRunData(BaseModel):
    """完整执行结果数据。"""
    run_id: str
    success: bool = Field(description="整体是否成功（全部 Agent 均成功则为 True）")
    agent_results: list[AgentTestResult] = Field(description="每个 Agent 的测试结果")
    steps_run: int
    plan_source: str = Field(default="", description="规划来源：rule_fallback / llm / compound_block")
    metrics: dict[str, Any] = Field(default_factory=dict, description="B8 评估指标（success/step_count/execution_success_rate 等）")


class PofficesRunResponse(BaseModel):
    """Poffices 完整执行响应。"""
    request_id: str
    api_version: str = "v1"
    code: str
    data: PofficesRunData | None = None
    error: PofficesError | None = None


def _extract_agent_results(
    trajectory: list[dict[str, Any]],
    fallback_agent: str,
    fallback_query: str,
) -> list[AgentTestResult]:
    """从轨迹中按 app_ready → send_query → get_response 分段提取每个 Agent 的测试结果。

    注意：
    - 规划器可能产出 **poffices_query**（填 query + 等待 + 抓取，与 get_response 语义重叠），
      必须与 get_response 一并识别，否则 agent_results 会为空。
    - 协作模式（agent_master_run_flow_once）单独处理：从 params.agents 提取实际参与的
      Agent 名称列表，避免只显示第一个 fallback_agent。
    """
    results: list[AgentTestResult] = []
    current_agent = fallback_agent
    current_query = fallback_query

    for entry in trajectory:
        step_result = entry.get("step_result") or {}
        tool_calls = step_result.get("tool_calls") or []
        execution_results = step_result.get("execution_results") or []

        for tc, er in zip(tool_calls, execution_results):
            tool_name = tc.get("tool_name", "")
            params = tc.get("params") or {}

            if tool_name == "app_ready":
                options = params.get("options") or {}
                if isinstance(options, dict):
                    current_agent = options.get("agent_name") or current_agent
                # 不重置 current_query：后续 send_query 会更新；若本段无 send_query，
                # 保持上一轮 query 比回退到可能无关的 fallback_query 更准确。

            elif tool_name == "send_query":
                current_query = params.get("query") or current_query

            elif tool_name in ("get_response", "poffices_query"):
                results.append(
                    AgentTestResult(
                        agent_name=current_agent,
                        query=current_query,
                        response=er.get("output_text"),
                        success=bool(er.get("success", False)),
                    )
                )

            elif tool_name == "agent_master_run_flow_once":
                # 协作模式：从 params.agents 提取所有参与 Agent 的显示名称
                agents_in_step = params.get("agents")
                if isinstance(agents_in_step, list) and agents_in_step:
                    agent_display = ", ".join(
                        str(a).strip() for a in agents_in_step if str(a).strip()
                    )
                else:
                    agent_display = current_agent
                results.append(
                    AgentTestResult(
                        agent_name=agent_display or current_agent,
                        query=current_query,
                        response=er.get("output_text"),
                        success=bool(er.get("success", False)),
                    )
                )

    return results


def _execute_run_sync(req: PofficesRunRequest) -> PofficesRunResponse:
    """同步执行完整 RPA 流程（在线程池中调用，避免阻塞事件循环）。"""
    import os
    from pathlib import Path as _Path

    _BASE = _Path(__file__).resolve().parent.parent.parent
    scenarios_dir = _BASE / "scenarios"
    log_dir = _BASE / "logs" / "poffices_api_run"
    log_dir.mkdir(parents=True, exist_ok=True)
    # 每次请求写入独立临时文件（以 request_id 命名），完成后原子重命名为 last.json，
    # 避免并发请求互相删除对方正在写入的文件。
    log_run_id = req.request_id

    run_id = req.request_id

    _append_trace({"event": "run_start", "request_id": run_id, "goal": req.goal[:200]})

    try:
        from raft.core.config.loader import load_experiment_config, load_task_spec
        from raft.orchestrator.runner import Orchestrator
        from raft.rpa import get_default_rpa

        # 加载基础配置（动态场景），然后用请求参数覆盖
        config_path = scenarios_dir / "experiment_poffices_dynamic.json"
        task_spec_path = scenarios_dir / "task_specs.json"
        config = load_experiment_config(config_path)
        task = load_task_spec(task_spec_path, config.task_spec_ids[0])

        # 用请求参数覆盖 extra
        config.extra["goal"] = req.goal
        config.extra["orchestration_mode"] = "goal_driven"
        # Poffices API 专用：单 agent 强制使用规则 planner（固定 3 步，防止 LLM planner
        # 多规划几轮导致待测 agent 被反复调用）；多 agent 仍启用 LLM planner 协调顺序。
        # 注意：此覆盖仅影响 /run 接口，不影响本地 run_poffices_agent.py 的开发流程。
        _n_agents_for_planner = len(req.agents_to_test) if req.agents_to_test else 1
        config.extra["use_llm_planner"] = _n_agents_for_planner > 1

        if req.llm_provider:
            config.extra["llm_provider"] = req.llm_provider
            config.extra["agent_provider"] = req.llm_provider
        if req.llm_model:
            config.extra["llm_model"] = req.llm_model
            config.extra["agent_model"] = req.llm_model

        # 处理 agents_to_test：
        # 未显式传时，用 goal_interpreter 从 goal 里抽取（否则会永远 fallback 到 Research Proposal）。
        # 候选 agent 列表显式传入，不依赖 goal_interpreter 的内置 _FALLBACK_AGENTS，保持 poffices 独立。
        agents = req.agents_to_test or []
        if not agents and req.goal:
            try:
                from raft.core.goal_interpreter import interpret_goal
                _poffices_available_agents = [
                    # Business Office
                    "Problem Statement", "Ideation", "Feasibility Analysis",
                    "Market Analysis", "Competitive Analysis", "Business Forecasting",
                    # 常见 Research / 其他 Office
                    "Research Proposal", "Project Proposal", "Marketing Plan",
                    "Business Forecasting Objective",
                ]
                _intent = interpret_goal(
                    req.goal,
                    available_agents=_poffices_available_agents,
                    provider=req.llm_provider,
                    model=req.llm_model,
                )
                if _intent.agents:
                    agents = [a for a in _intent.agents if isinstance(a, str) and a.strip()]
            except Exception as _exc:
                logger.warning("[poffices /run] goal_interpreter 解析失败，回退默认 agent：%s", _exc)
        if agents:
            config.extra["agents_to_test"] = agents
            config.extra["agent_under_test"] = agents[0]
            config.extra["agent_descriptor"] = (
                f"Poffices 的 {agents[0]} Agent"
                if len(agents) == 1
                else f"Poffices 的 {len(agents)} 个 Agent（{', '.join(agents[:3])}）"
            )
        else:
            # 没指定 agents，让 goal interpreter 推断（已在 extra.goal 里）；
            # 直接赋值（与有 agents 时的逻辑对称），确保请求参数始终覆盖配置文件预设值。
            config.extra["agent_under_test"] = config.extra.get("agent_under_test") or "Research Proposal"
            config.extra["agent_descriptor"] = config.extra.get("agent_descriptor") or "Poffices 的 Research Proposal Agent"

        # 处理 query
        if req.query:
            # 固定 query，跳过 LLM 生成
            config.extra["use_llm_query"] = False
            task = task.model_copy(update={"initial_state": {**task.initial_state, "query": req.query}})
        else:
            config.extra["use_llm_query"] = True

        # Poffices API 专用：步数上限 = 每 agent 3 步 + 2 步容错（replan/retry）。
        # 比本地开发的宽松上限更严格，防止单 agent 场景下 orchestrator 跑超过一轮。
        n_agents = len(agents) if agents else 1
        max_steps = n_agents * 3 + 2

        rpa = get_default_rpa(backend="poffices", headless=False, timeout_ms=30_000, query_wait_sec=240)

        orch = Orchestrator(
            max_steps=max_steps,
            rpa=rpa,
            orchestration_mode="goal_driven",
        )

        try:
            result = orch.run_until_done(config, task, run_id=log_run_id, log_dir=log_dir)
        finally:
            rpa.close()

        # 落盘用 log_run_id（即 request_id），响应与 metrics 仍使用真实 request_id
        if isinstance(result, dict):
            result["run_id"] = run_id
            _m = result.get("metrics")
            if isinstance(_m, dict):
                _m["run_id"] = run_id

        # 原子重命名为 last.json，方便调试时快速定位最新结果；
        # 清理除 last.json 之外的其他旧请求文件，避免目录无限增长。
        _written = log_dir / f"{log_run_id}.json"
        _last = log_dir / "last.json"
        try:
            if _written.exists():
                _written.replace(_last)  # 同文件系统内原子替换
        except OSError:
            pass
        for _stale in log_dir.glob("*.json"):
            if _stale.name == "last.json":
                continue
            try:
                _stale.unlink()
            except OSError:
                pass

        # 提取结果
        trajectory = result.get("trajectory") or []
        fallback_agent = agents[0] if agents else config.extra.get("agent_under_test", "Unknown")
        # 从轨迹里取第一个实际用到的 query
        fallback_query = req.query or ""
        if not fallback_query and trajectory:
            first_snap = ((trajectory[0].get("step_result") or {}).get("agent_input_snapshot") or {})
            fallback_query = (first_snap.get("state") or {}).get("query") or req.goal

        agent_results = _extract_agent_results(trajectory, fallback_agent, fallback_query)
        overall_success = bool(result.get("metrics", {}).get("success", False))

        _append_trace({
            "event": "run_ok",
            "request_id": run_id,
            "steps_run": result.get("steps_run", 0),
            "agent_count": len(agent_results),
            "success": overall_success,
        })

        return PofficesRunResponse(
            request_id=run_id,
            code="ok",
            data=PofficesRunData(
                run_id=run_id,
                success=overall_success,
                agent_results=agent_results,
                steps_run=result.get("steps_run", 0),
                plan_source=result.get("plan_source") or "unknown",
                metrics=result.get("metrics") or {},
            ),
        )

    except Exception as exc:
        _append_trace({"event": "run_exception", "request_id": run_id, "error": str(exc)})
        return PofficesRunResponse(
            request_id=run_id,
            code="run_failed",
            data=None,
            error=PofficesError(
                code="run_failed",
                message=str(exc),
                details={"goal": req.goal},
            ),
        )


@router.post(
    "/run",
    summary="Poffices 完整执行 — 规划 + RPA + 返回测试结果",
    response_model=None,
)
async def poffices_run(request: Request) -> PofficesRunResponse | JSONResponse:
    """
    完整执行接口：接收 goal（+ 可选 agents/query），在本地 **规划 → Playwright RPA 执行 → 返回结构化结果**。

    供 Poffices 画布的 Custom Block 调用：拿到测试结果后可直接经 AOA 传给同学的评估 Agent。

    响应默认包成 **chat.completion**（`choices[0].message.content` 为完整执行结果 JSON），
    且顶层附带 **extracted_data**（与 `message.content` 解析后一致的结构化对象），对齐参考实现中的
    数据传输方式，便于画布直接绑定而不只依赖嵌套字符串。调试原生结构可传 `response_envelope: "native"`。

    **注意**：执行时间取决于 Poffices Agent 响应速度，通常 30–240 秒，请在 Poffices API Management
    中将此 Custom Block 的超时设置调高（建议 ≥ 300 秒）。
    """
    import asyncio

    raw_bytes = await request.body()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail="Invalid UTF-8 body") from e
    try:
        raw = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e

    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    _hdrs = {
        k: request.headers.get(k)
        for k in (
            "user-agent", "x-forwarded-for", "x-real-ip", "origin", "referer",
            "x-request-id", "x-correlation-id", "x-poffices-agent-id",
            "x-poffices-run-id", "x-poffices-node-id", "authorization",
            "host", "cf-connecting-ip", "forwarded",
        )
        if request.headers.get(k)
    }
    _append_trace({
        "event": "run_request_in",
        "client": getattr(request.client, "host", None),
        "headers": _hdrs,
        "raw_preview": _truncate(json.dumps(raw, ensure_ascii=False)),
    })

    # 兼容 OpenAI Chat 形态：从 messages 里提取 goal；与 /plan 保持相同的归一化链路
    normalized = _coerce_bools(_normalize_body_for_plan(raw))
    try:
        from pydantic import ValidationError as _VE
        req = PofficesRunRequest.model_validate(normalized)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    openai_compat = raw.get("response_envelope") != "native"
    is_stream = bool(raw.get("stream"))
    model_name = raw.get("model") if isinstance(raw.get("model"), str) else None

    # 流式路径：边跑边推心跳，避免 ngrok/Poffices 空闲超时切断连接
    if openai_compat and is_stream:
        _append_trace({"event": "response_sse_stream", "request_id": req.request_id})
        return StreamingResponse(
            _sse_run_with_heartbeat(req, model=model_name),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式路径：老逻辑
    loop = asyncio.get_running_loop()
    resp = await loop.run_in_executor(None, _execute_run_sync, req)

    if openai_compat:
        resp_dict = resp.model_dump()
        body = json.dumps(resp_dict, ensure_ascii=False)
        _append_trace({"event": "response_openai_chat_completion", "request_id": req.request_id})
        return JSONResponse(
            _openai_chat_completion_dict(
                content=body,
                model=model_name,
                extracted_data=resp_dict,
            )
        )
    return resp


# ── /run_full：完整本地流水线（多轮 + LLM 报告 → HTML 作为 content） ──────────


class PofficesRunFullRequest(PofficesRunRequest):
    """完整本地流水线请求：支持多轮，返回 HTML 报告作为 content。"""
    rounds: int = Field(default=1, ge=1, le=10, description="执行轮数（对齐本地 --runs）")
    use_llm_summary: bool = Field(
        default=True,
        description="是否调用 LLM 生成多轮分析总结（对齐本地 --full-report）",
    )
    minimal_report: bool = Field(
        default=False,
        description="True 则不出 LLM 总结与判分，只出结构化 HTML（对齐本地 mini 报告）",
    )


class PofficesRunFullData(BaseModel):
    """完整流水线结果：HTML 报告 + 每轮结构化信息。"""
    run_id: str
    success: bool = Field(description="所有轮全部成功才为 True")
    rounds_run: int
    agent_results: list[AgentTestResult] = Field(default_factory=list)
    metrics_per_round: list[dict[str, Any]] = Field(default_factory=list)
    report_html: str = Field(default="", description="完整 HTML 报告（与本地 run_report.html 等价）；SSE 路径下会被清空以减小 payload")
    report_url: str = Field(default="", description="公开可访问的 HTML 报告 URL，供 Poffices 画布下游节点拼 markdown 链接")
    llm_summary: str | None = Field(default=None, description="LLM 多轮分析总结（若启用）")
    report_path: str | None = Field(default=None, description="服务器端落盘路径，便于排查")


class PofficesRunFullResponse(BaseModel):
    request_id: str
    api_version: str = "v1"
    code: str
    data: PofficesRunFullData | None = None
    error: PofficesError | None = None


def _previous_rounds_for_api(results: list[dict]) -> list[dict]:
    """从多轮 results 构建 previous_rounds（给 query_suggester 感知历史）。

    精简版：复用 run_poffices_agent._previous_rounds_from_results 的字段子集；
    不包含 failed_steps 等细粒度分析，保持 API 侧零额外依赖。
    """
    rounds: list[dict] = []
    for r in results:
        metrics = r.get("metrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}
        # 取该轮实际发送的 query：首个 send_query.params.query
        q = ""
        for entry in r.get("trajectory") or []:
            for tc in (entry.get("step_result") or {}).get("tool_calls") or []:
                if tc.get("tool_name") == "send_query":
                    qv = (tc.get("params") or {}).get("query")
                    if isinstance(qv, str) and qv.strip():
                        q = qv
                        break
            if q:
                break
        rounds.append({
            "query": q,
            "success": metrics.get("success"),
            "step_count": metrics.get("step_count"),
            "details": metrics.get("details"),
            "llm_judge": metrics.get("llm_judge"),
            "execution_success_rate": metrics.get("execution_success_rate"),
            "retry_count": metrics.get("retry_count"),
            "timeout_count": metrics.get("timeout_count"),
        })
    return rounds


def _execute_run_full_sync(req: PofficesRunFullRequest) -> PofficesRunFullResponse:
    """同步执行完整流水线：多轮 RPA + LLM 多轮总结 + HTML 报告。"""
    from pathlib import Path as _Path

    _BASE = _Path(__file__).resolve().parent.parent.parent
    scenarios_dir = _BASE / "scenarios"
    log_dir = _BASE / "logs" / "poffices_api_run_full"
    log_dir.mkdir(parents=True, exist_ok=True)

    run_id = req.request_id
    _append_trace({"event": "run_full_start", "request_id": run_id, "rounds": req.rounds, "goal": req.goal[:200]})

    try:
        from raft.core.config.loader import load_experiment_config, load_task_spec
        from raft.orchestrator.runner import Orchestrator
        from raft.reporting import build_report_with_llm
        from raft.rpa import get_default_rpa

        config_path = scenarios_dir / "experiment_poffices_dynamic.json"
        task_spec_path = scenarios_dir / "task_specs.json"
        config = load_experiment_config(config_path)
        task = load_task_spec(task_spec_path, config.task_spec_ids[0])

        config.extra["goal"] = req.goal
        config.extra["orchestration_mode"] = "goal_driven"

        _n_agents_for_planner = len(req.agents_to_test) if req.agents_to_test else 1
        config.extra["use_llm_planner"] = _n_agents_for_planner > 1

        if req.llm_provider:
            config.extra["llm_provider"] = req.llm_provider
            config.extra["agent_provider"] = req.llm_provider
        if req.llm_model:
            config.extra["llm_model"] = req.llm_model
            config.extra["agent_model"] = req.llm_model

        # agents_to_test 解析（与 /run 保持一致）
        agents = req.agents_to_test or []
        if not agents and req.goal:
            try:
                from raft.core.goal_interpreter import interpret_goal
                _available = [
                    "Problem Statement", "Ideation", "Feasibility Analysis",
                    "Market Analysis", "Competitive Analysis", "Business Forecasting",
                    "Research Proposal", "Project Proposal", "Marketing Plan",
                    "Business Forecasting Objective",
                ]
                _intent = interpret_goal(
                    req.goal,
                    available_agents=_available,
                    provider=req.llm_provider,
                    model=req.llm_model,
                )
                if _intent.agents:
                    agents = [a for a in _intent.agents if isinstance(a, str) and a.strip()]
            except Exception as _exc:
                logger.warning("[poffices /run_full] goal_interpreter 解析失败：%s", _exc)

        if agents:
            config.extra["agents_to_test"] = agents
            config.extra["agent_under_test"] = agents[0]
            config.extra["agent_descriptor"] = (
                f"Poffices 的 {agents[0]} Agent"
                if len(agents) == 1
                else f"Poffices 的 {len(agents)} 个 Agent（{', '.join(agents[:3])}）"
            )
        else:
            config.extra["agent_under_test"] = config.extra.get("agent_under_test") or "Research Proposal"
            config.extra["agent_descriptor"] = config.extra.get("agent_descriptor") or "Poffices 的 Research Proposal Agent"

        if req.query:
            config.extra["use_llm_query"] = False
            task = task.model_copy(update={"initial_state": {**task.initial_state, "query": req.query}})
        else:
            config.extra["use_llm_query"] = True

        n_agents = len(agents) if agents else 1
        max_steps = n_agents * 3 + 2

        rpa = get_default_rpa(backend="poffices", headless=False, timeout_ms=30_000, query_wait_sec=240)

        results: list[dict] = []
        try:
            for i in range(req.rounds):
                orch = Orchestrator(
                    max_steps=max_steps,
                    rpa=rpa,
                    orchestration_mode="goal_driven",
                )
                sub_run_id = f"{run_id}-r{i + 1}"

                query_context: dict[str, Any] = {}
                if i > 0 and results:
                    prev_rounds = _previous_rounds_for_api(results)
                    query_context["previous_rounds"] = prev_rounds
                    query_context["previous_queries"] = [r.get("query") for r in prev_rounds if r.get("query")]
                    if i == 1:
                        query_context["multi_round_strategy"] = "diversify"
                        query_context["policy_hint"] = "本轮请换一个与上一轮完全不同的领域或话题，以考察 Agent 的多样化能力。"
                    elif i >= 2 and req.rounds >= 3:
                        try:
                            from raft.core.query_policy import decide_next_strategy
                            strategy, policy_hint = decide_next_strategy(prev_rounds)
                            query_context["multi_round_strategy"] = strategy
                            query_context["policy_hint"] = policy_hint
                        except Exception:
                            pass

                result = orch.run_until_done(
                    config,
                    task,
                    run_id=sub_run_id,
                    log_dir=log_dir,
                    query_context=query_context or None,
                )
                if isinstance(result, dict):
                    result["run_id"] = sub_run_id
                results.append(result)
        finally:
            rpa.close()

        # 报告生成
        task_for_report = (results[-1].get("task_spec_effective") if results else None) or task.model_dump()
        config_dump = config.model_dump()
        report_path = log_dir / f"{run_id}.html"
        _use_llm_summary = req.use_llm_summary and not req.minimal_report
        out = build_report_with_llm(
            results,
            config_dump,
            task_for_report,
            output_path=report_path,
            use_llm_summary=_use_llm_summary,
            minimal_report=req.minimal_report,
        )
        report_html = out.get("html") or ""
        llm_summary = out.get("llm_summary")

        # 聚合每轮 agent_results
        fallback_agent = agents[0] if agents else config.extra.get("agent_under_test", "Unknown")
        all_agent_results: list[AgentTestResult] = []
        metrics_per_round: list[dict[str, Any]] = []
        overall_success = True
        for r in results:
            traj = r.get("trajectory") or []
            fq = req.query or ""
            if not fq and traj:
                first_snap = ((traj[0].get("step_result") or {}).get("agent_input_snapshot") or {})
                fq = (first_snap.get("state") or {}).get("query") or req.goal
            all_agent_results.extend(_extract_agent_results(traj, fallback_agent, fq))
            m = r.get("metrics") or {}
            metrics_per_round.append(m if isinstance(m, dict) else {})
            if not (isinstance(m, dict) and m.get("success")):
                overall_success = False
        if not results:
            overall_success = False

        _append_trace({
            "event": "run_full_ok",
            "request_id": run_id,
            "rounds_run": len(results),
            "agent_count": len(all_agent_results),
            "success": overall_success,
            "llm_summary_len": len(llm_summary) if isinstance(llm_summary, str) else 0,
            "report_len": len(report_html),
        })

        return PofficesRunFullResponse(
            request_id=run_id,
            code="ok",
            data=PofficesRunFullData(
                run_id=run_id,
                success=overall_success,
                rounds_run=len(results),
                agent_results=all_agent_results,
                metrics_per_round=metrics_per_round,
                report_html=report_html,
                llm_summary=llm_summary,
                report_path=str(report_path) if report_path.exists() else None,
            ),
        )

    except Exception as exc:
        _append_trace({"event": "run_full_exception", "request_id": run_id, "error": str(exc)})
        return PofficesRunFullResponse(
            request_id=run_id,
            code="run_failed",
            data=None,
            error=PofficesError(
                code="run_failed",
                message=str(exc),
                details={"goal": req.goal},
            ),
        )


_REPORT_DIR = Path(__file__).resolve().parent.parent.parent / "logs" / "poffices_api_run_full"


def _public_base_url(request: Request | None) -> str:
    """拼公开可访问的 base URL（给 Poffices 画布用）。

    优先读 env `POFFICES_PUBLIC_BASE_URL`（如 ngrok 域名）；没设置则从 request 拼
    scheme+host，用 X-Forwarded-* 头以兼容反代。末尾不带斜杠。
    """
    env = os.environ.get("POFFICES_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if env:
        return env
    if request is None:
        return ""
    fwd_proto = request.headers.get("x-forwarded-proto")
    fwd_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    scheme = fwd_proto or request.url.scheme
    host = fwd_host or request.url.netloc
    return f"{scheme}://{host}".rstrip("/")


def _build_report_markdown(
    *,
    report_url: str,
    resp: "PofficesRunFullResponse",
    embed_iframe: bool = True,
) -> str:
    """把 HTML 报告包装成 Poffices 画布友好的 markdown：链接 + 关键指标 + 可选 iframe。

    Poffices 的 Custom Block 大多把 content 当 markdown 渲染；直接塞 HTML 源码会被当文字显示。
    这里给出：
    - 顶部一个标题
    - 关键指标（成功轮数 / 轮数 / Agent 数）
    - 「查看完整报告」超链接（新窗口打开）
    - iframe 内嵌（若 Poffices 的渲染器支持 HTML 透传，画布内直接可滚动阅读报告）
    """
    data = resp.data
    lines: list[str] = []
    lines.append("## RPA 测试报告")
    if data:
        ok_rounds = sum(1 for m in data.metrics_per_round if isinstance(m, dict) and m.get("success"))
        lines.append("")
        lines.append(f"- 总轮数：**{data.rounds_run}**，成功：**{ok_rounds}**")
        lines.append(f"- 参与 Agent 数：**{len({r.agent_name for r in data.agent_results})}**")
        lines.append(f"- 整体成功：**{'✅' if data.success else '❌'}**")
        if data.llm_summary:
            snippet = data.llm_summary.strip().replace("\n", " ")
            if len(snippet) > 280:
                snippet = snippet[:280] + "…"
            lines.append("")
            lines.append("**LLM 多轮分析摘要**：" + snippet)
    if report_url:
        lines.append("")
        lines.append(f"📄 [点击查看完整 HTML 报告（新窗口）]({report_url})")
        if embed_iframe:
            lines.append("")
            lines.append(
                f'<iframe src="{report_url}" width="100%" height="720" '
                f'style="border:1px solid #ddd;border-radius:8px;"></iframe>'
            )
    else:
        lines.append("")
        lines.append("⚠️ 报告 URL 未生成（未配置 POFFICES_PUBLIC_BASE_URL 且无法从请求头推断）。")
    return "\n".join(lines)


@router.get(
    "/report/{request_id}",
    summary="Poffices 完整流水线 — HTML 报告静态下载",
    response_class=HTMLResponse,
)
def poffices_report(request_id: str) -> HTMLResponse:
    """按 request_id 返回 `/run_full` 落盘的 HTML 报告。

    防路径穿越：request_id 只允许 [A-Za-z0-9_-]+；其余字符一律 404。
    """
    import re

    if not re.fullmatch(r"[A-Za-z0-9_\-]+", request_id or ""):
        raise HTTPException(status_code=404, detail="report not found")
    path = _REPORT_DIR / f"{request_id}.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="report not found")
    try:
        html = path.read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"read failed: {e}") from e
    # 允许 iframe 嵌入：不设 X-Frame-Options DENY；Content-Security-Policy 留空交由上游反代
    return HTMLResponse(content=html, status_code=200)


async def _sse_run_full_with_heartbeat(
    req: "PofficesRunFullRequest",
    *,
    model: str | None,
    public_base_url: str,
    heartbeat_sec: float = 5.0,
    body_chunk_size: int = 2000,
):
    """流式推送完整 HTML 报告。心跳用真 content delta，避免代理判定为空闲超时。"""
    import asyncio

    resp_id = f"chatcmpl-{int(time.time())}"

    def _make_chunk(
        content: str | None = None,
        finish: bool = False,
        extracted: dict[str, Any] | None = None,
    ) -> str:
        delta = {"content": content} if content is not None else {}
        payload: dict[str, Any] = {
            "id": resp_id,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": "stop" if finish else None,
                }
            ],
        }
        if extracted is not None:
            payload["extracted_data"] = extracted
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield _make_chunk(content="🤖 RPA 全流程（含报告）启动...\n")

    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(None, _execute_run_full_sync, req)

    while True:
        try:
            resp = await asyncio.wait_for(asyncio.shield(task), timeout=heartbeat_sec)
            break
        except asyncio.TimeoutError:
            yield _make_chunk(content=".")

    # 关键契约：content = 紧凑 JSON 字符串（对齐 /run 的载荷形态，Poffices openllm handler
    # 才能把 {layer_name_X_output} 稳定透给下游节点）。HTML 报告体积过大，不塞进 content；
    # 下游只需要 report_url，点开即可查看本地渲染的完整 HTML 报告。
    report_url = (
        f"{public_base_url}/api/v1/poffices/report/{req.request_id}"
        if public_base_url
        else ""
    )
    if resp.data is not None:
        resp.data.report_url = report_url
        resp.data.report_html = ""  # 流模式下剔除 HTML 以缩小 payload
    resp_dict = resp.model_dump()
    resp_dict["report_url"] = report_url  # 顶层冗余一份，extracted_data 也能直接取
    body = json.dumps(resp_dict, ensure_ascii=False)

    yield _make_chunk(content="\n\n")
    for i in range(0, len(body), body_chunk_size):
        yield _make_chunk(content=body[i : i + body_chunk_size])

    yield _make_chunk(content="", finish=True, extracted=resp_dict)
    yield "data: [DONE]\n\n"


@router.post(
    "/run_full",
    summary="Poffices 完整流水线 — 多轮 RPA + LLM 多轮总结 + HTML 报告",
    response_model=None,
)
async def poffices_run_full(request: Request) -> PofficesRunFullResponse | JSONResponse:
    """
    与本地 `run_poffices_agent.py` 对齐的完整流水线接口：
    接收 goal（+ 可选 agents/query/rounds/use_llm_summary），在本地依次
    **规划 → 多轮 Playwright RPA → LLM 多轮分析 → HTML 报告**，
    最终把完整 HTML 报告作为 `choices[0].message.content` 返回，供 Poffices
    Custom Block 画布直接展示；顶层 `extracted_data` 同时提供结构化字段。

    与 `/run` 的区别：
    - `/run`：单轮、结构化 agent_results，给下游 Agent（评分等）接力。
    - `/run_full`：多轮、HTML 报告，**不**适合给下一个 Agent 解析——是给人看的。

    **超时提示**：多轮 + LLM 总结可能 > 5 分钟，务必调大上游超时。
    """
    import asyncio

    raw_bytes = await request.body()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=400, detail="Invalid UTF-8 body") from e
    try:
        raw = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e

    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    _hdrs = {
        k: request.headers.get(k)
        for k in (
            "user-agent", "x-forwarded-for", "x-real-ip", "origin", "referer",
            "x-request-id", "x-correlation-id", "x-poffices-agent-id",
            "x-poffices-run-id", "x-poffices-node-id", "authorization",
            "host", "cf-connecting-ip", "forwarded",
        )
        if request.headers.get(k)
    }
    _append_trace({
        "event": "run_full_request_in",
        "client": getattr(request.client, "host", None),
        "headers": _hdrs,
        "raw_preview": _truncate(json.dumps(raw, ensure_ascii=False)),
    })

    normalized = _coerce_bools(_normalize_body_for_plan(raw))
    try:
        req = PofficesRunFullRequest.model_validate(normalized)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    openai_compat = raw.get("response_envelope") != "native"
    is_stream = bool(raw.get("stream"))
    model_name = raw.get("model") if isinstance(raw.get("model"), str) else None

    public_base = _public_base_url(request)

    if openai_compat and is_stream:
        _append_trace({"event": "run_full_response_sse_stream", "request_id": req.request_id})
        return StreamingResponse(
            _sse_run_full_with_heartbeat(req, model=model_name, public_base_url=public_base),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    loop = asyncio.get_running_loop()
    resp = await loop.run_in_executor(None, _execute_run_full_sync, req)

    if openai_compat:
        report_url = (
            f"{public_base}/api/v1/poffices/report/{req.request_id}" if public_base else ""
        )
        if resp.data is not None:
            resp.data.report_url = report_url
            resp.data.report_html = ""  # chat.completion 路径也剔除 HTML，缩小 payload
        resp_dict = resp.model_dump()
        resp_dict["report_url"] = report_url
        body = json.dumps(resp_dict, ensure_ascii=False)
        _append_trace({
            "event": "run_full_response_openai_chat_completion",
            "request_id": req.request_id,
            "report_url": report_url,
        })
        return JSONResponse(
            _openai_chat_completion_dict(
                content=body,
                model=model_name,
                extracted_data=resp_dict,
            )
        )
    return resp
