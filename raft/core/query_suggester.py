"""
单轮 Query 建议器：根据任务规范与待测 Agent 描述，用 LLM 生成一条用于评估的 query。
供 B9 在 run 开始前调用（use_llm_query 时）；多轮时可传入上一轮 query 与 Agent 表现，根据「问什么 + 表现如何」决定下一轮问什么、怎么问。
支持 OpenAI、Qwen、Grok（xAI）等 OpenAI 兼容 API。
"""
import os
import re
from typing import Any, Literal

from raft.contracts.models import TaskSpec

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

from raft.core.llm_client import chat_completion_with_retry
from raft.core.llm_providers import normalize_provider, resolve_api_key, resolve_base_url, resolve_chat_model


def _resolve_suggester_llm(
    provider: str | None,
    model: str | None,
    base_url: str | None,
) -> tuple[str, str, str | None]:
    """返回 (规范化 provider, model, base_url)。"""
    _prov = normalize_provider(provider)
    _b = resolve_base_url(_prov, base_url)
    _m = resolve_chat_model(_prov, model, base_url_override=base_url)
    return _prov, _m, _b

MultiRoundStrategy = Literal["deepen", "diversify", "auto"]
"""多轮策略：deepen=同领域更细致，diversify=换领域，auto=由 LLM 二选一。"""

# 所有出题 prompt 共用：要求 query 含具体信息，避免泛化表述，便于 Agent 执行与评估
_QUERY_CONCRETENESS_RULE = (
    "query 必须包含**具体信息**（如具体行业、地区、时间、业务对象等），"
    "避免泛化表述如「我们公司」「某部门」「请分析…及改进建议」等；以便 Agent 可执行、可评估。"
)


def _format_scenario_context(scenario_context: str | None) -> str:
    if not scenario_context or not str(scenario_context).strip():
        return ""
    return f"场景规范：\n{str(scenario_context).strip()}\n\n"


def _format_goal(goal: str | None) -> str:
    if not goal or not str(goal).strip():
        return ""
    return f"用户目标（必须体现在 query 中）：{str(goal).strip()}\n\n"


def _build_prompt(
    task_spec: TaskSpec,
    agent_descriptor: str,
    scenario_context: str | None = None,
    goal: str | None = None,
) -> str:
    """构建发给 LLM 的出题 prompt（首轮无历史）。"""
    desc = (task_spec.description or "").strip()
    task_id = task_spec.task_spec_id or ""
    goal_block = _format_goal(goal)
    return (
        "你负责为「待测 Agent」设计一条测试用的 query（用户输入），用于评估该 Agent 的表现。\n\n"
        f"任务 ID：{task_id}\n"
        f"任务描述：{desc}\n"
        f"{goal_block}"
        f"{_format_scenario_context(scenario_context)}"
        f"待测 Agent 描述：{agent_descriptor}\n\n"
        "请根据上述信息，设计一条简洁、能有效考察该 Agent 能力的中文 query。"
        f"{_QUERY_CONCRETENESS_RULE}\n"
        "只回复这一条 query 本身，不要解释、不要引号、不要换行。长度控制在 30 字以内。"
    )


def _build_prompt_multi_round(
    task_spec: TaskSpec,
    agent_descriptor: str,
    previous_queries: list[str],
    strategy: MultiRoundStrategy,
    scenario_context: str | None = None,
    goal: str | None = None,
) -> str:
    """多轮时构建 prompt：基于已有 query 生成「深化」或「换领域」的新 query。"""
    desc = (task_spec.description or "").strip()
    task_id = task_spec.task_spec_id or ""
    prev_list = "\n".join(f"  - {q}" for q in previous_queries if (q and str(q).strip()))
    if not prev_list:
        return _build_prompt(task_spec, agent_descriptor, scenario_context=scenario_context, goal=goal)

    strategy_instruction = {
        "deepen": (
            "本轮请**在同一领域/话题**下，给出一条更细致、更具体、更深度的测试 query（例如从宏观问到微观、从概况问到细节），以考察 Agent 在同一领域的深入能力。"
        ),
        "diversify": (
            "本轮请**换一个与上述 query 完全不同的领域或话题**，设计一条新 query，以考察该 Agent 在多样化场景下的能力（避免重复房地产、市场分析等同一类问题）。"
        ),
        "auto": (
            "本轮请从以下两种策略中**任选一种**并生成一条新 query：\n"
            "  A) **深化**：在同一领域/话题下，给出更细致、更具体、更深度的 query；\n"
            "  B) **换领域**：换一个与上述 query 完全不同的领域或话题，设计新 query。\n"
            "只选一种执行，不要两种都做。目的是避免多轮 query 大同小异（例如都问房地产），要么同领域更深入，要么换领域测多样化能力。"
        ),
    }
    instr = strategy_instruction.get(strategy, strategy_instruction["auto"])
    goal_block = _format_goal(goal)

    return (
        "你负责为「待测 Agent」在多轮测试中设计**本轮的**测试 query。\n\n"
        f"任务 ID：{task_id}\n"
        f"任务描述：{desc}\n"
        f"{goal_block}"
        f"{_format_scenario_context(scenario_context)}"
        f"待测 Agent 描述：{agent_descriptor}\n\n"
        "以下 query 已在**之前轮次**使用过，请勿重复或仅做轻微改写：\n"
        f"{prev_list}\n\n"
        f"{instr}\n\n"
        f"{_QUERY_CONCRETENESS_RULE}\n"
        "只回复这一条新 query 本身，不要解释、不要引号、不要换行。长度控制在 40 字以内。"
    )


def _format_round_performance(round_data: dict[str, Any], index: int) -> str:
    """将单轮「query + 表现」格式化为一段可读文本，供 prompt 使用。"""
    parts = [f"【轮次 {index}】"]
    q = round_data.get("query") or ""
    if q:
        parts.append(f"  问题：{q[:80]}{'…' if len(q) > 80 else ''}")
    success = round_data.get("success")
    if success is not None:
        parts.append(f"  任务结果：{'成功' if success else '失败'}")
    step_count = round_data.get("step_count")
    if step_count is not None:
        parts.append(f"  步数：{step_count}")
    details = round_data.get("details") or {}
    # 支持 metrics 在顶层或 details 内
    exec_rate = round_data.get("execution_success_rate") or (details.get("execution_success_rate") if isinstance(details, dict) else None)
    if exec_rate is not None:
        try:
            parts.append(f"  执行成功率：{float(exec_rate):.0%}")
        except (TypeError, ValueError):
            pass
    retry = round_data.get("retry_count")
    if retry is None and isinstance(details, dict):
        retry = details.get("retry_count")
    if retry is not None and int(retry) > 0:
        parts.append(f"  重试次数：{int(retry)}")
    timeout = round_data.get("timeout_count")
    if timeout is None and isinstance(details, dict):
        timeout = details.get("timeout_count")
    if timeout is not None and int(timeout) > 0:
        parts.append(f"  超时次数：{int(timeout)}")
    llm_judge = round_data.get("llm_judge")
    if isinstance(llm_judge, dict):
        comment = llm_judge.get("output_comment")
        if comment and str(comment).strip():
            parts.append(f"  LLM 评语：{str(comment).strip()[:120]}{'…' if len(str(comment)) > 120 else ''}")
        scores = []
        for k in ("decision_quality", "output_quality", "tool_proficiency"):
            v = llm_judge.get(k)
            if v is not None:
                try:
                    scores.append(f"{k}={float(v):.2f}")
                except (TypeError, ValueError):
                    pass
        if scores:
            parts.append("  评分：" + ", ".join(scores))
    failed_steps = round_data.get("failed_steps")
    if isinstance(failed_steps, list) and failed_steps:
        tool_names = [s.get("tool_name") for s in failed_steps if s.get("tool_name")]
        err_types = [s.get("error_type") for s in failed_steps if s.get("error_type")]
        if tool_names:
            unique_tools = list(dict.fromkeys(tool_names))
            parts.append(f"  失败工具: {', '.join(unique_tools)}")
        if err_types:
            unique_errs = list(dict.fromkeys(err_types))
            parts.append(f"  错误类型: {', '.join(unique_errs)}")
    return "\n".join(parts)


def _build_prompt_with_performance(
    task_spec: TaskSpec,
    agent_descriptor: str,
    previous_rounds: list[dict[str, Any]],
    strategy: MultiRoundStrategy,
    *,
    policy_hint: str | None = None,
    scenario_context: str | None = None,
    goal: str | None = None,
) -> str:
    """多轮且带表现时：根据「上一轮问题 + Agent 表现」决定下一轮问什么、怎么问。若提供 policy_hint 则优先使用（规则策略）。"""
    desc = (task_spec.description or "").strip()
    task_id = task_spec.task_spec_id or ""
    rounds_text = "\n\n".join(
        _format_round_performance(r, i + 1) for i, r in enumerate(previous_rounds) if r
    )
    if not rounds_text.strip():
        return _build_prompt(task_spec, agent_descriptor, scenario_context=scenario_context, goal=goal)

    goal_block = _format_goal(goal)
    if policy_hint and str(policy_hint).strip():
        hint = str(policy_hint).strip()
    else:
        strategy_hint = {
            "deepen": "可优先在同一领域给出更细致、更具体的 query，考察深入能力。",
            "diversify": "可优先换一个与之前完全不同的领域或话题，考察多样化能力。",
            "auto": (
                "请根据上述表现**自行决定**下一轮问什么、怎么问。例如：\n"
                "  - 某领域表现好：可深化（同领域更细）或换领域测广度；\n"
                "  - 某领域表现差：可简化难度、换角度再测、或根据 LLM 评语针对性出题；\n"
                "  - 避免多轮问题大同小异；既要考察深度也要考察广度与鲁棒性。"
            ),
        }
        hint = strategy_hint.get(strategy, strategy_hint["auto"])

    return (
        "你负责为「待测 Agent」在多轮测试中设计**下一轮**的测试 query。\n\n"
        f"任务 ID：{task_id}\n"
        f"任务描述：{desc}\n"
        f"{goal_block}"
        f"{_format_scenario_context(scenario_context)}"
        f"待测 Agent 描述：{agent_descriptor}\n\n"
        "以下是**之前各轮**的「问题」与「Agent 表现」摘要，请据此决定下一轮问什么、怎么问：\n\n"
        f"{rounds_text}\n\n"
        f"要求：{hint}\n\n"
        f"{_QUERY_CONCRETENESS_RULE}\n"
        "只回复**一条**新 query 本身，不要解释、不要引号、不要换行。长度控制在 40 字以内。"
    )


# 多轮且需写入报告时，让 LLM 同时输出「选择思路」的 prompt 后缀
_RATIONALE_PROMPT_SUFFIX = (
    "\n\n请回复**两行**。"
    "第一行：仅写一条 query，不要引号、不要其他文字。"
    "第二行：以「选择思路：」开头，用一句话说明为何选此 query（例如：上一轮房地产表现好，本轮深化同领域；或上一轮失败，本轮换领域考察）。"
)


def _parse_query(text: str) -> str:
    """从 LLM 回复中提取单行 query，去除多余引号与空白。"""
    text = (text or "").strip()
    text = re.sub(r'^["\']|["\']$', "", text)
    text = text.split("\n")[0].strip()
    return text if text else ""


def _build_prompt_multi_agent(
    task_spec: TaskSpec,
    agent_descriptor: str,
    agents_list: list[str],
    scenario_context: str | None = None,
    goal: str | None = None,
) -> str:
    """多 Agent 时：为每个 Agent 各设计一条 query，与 scenario/任务描述/用户目标联动。"""
    desc = (task_spec.description or "").strip()
    task_id = task_spec.task_spec_id or ""
    goal_block = _format_goal(goal)
    agents_text = "\n".join(f"  {i + 1}. {a}" for i, a in enumerate(agents_list))
    return (
        "你负责为「多 Agent 测试」设计测试 query。本 run 将依次测试以下 N 个 Agent，请为**每个** Agent 分别设计一条适合该 Agent 能力的测试 query。\n\n"
        f"任务 ID：{task_id}\n"
        f"任务描述：{desc}\n"
        f"{goal_block}"
        f"{_format_scenario_context(scenario_context)}"
        f"待测 Agent 描述：{agent_descriptor}\n\n"
        "待测 Agent 列表（按顺序）：\n"
        f"{agents_text}\n\n"
        "请根据任务描述、用户目标与各 Agent 名称/职责，为每个 Agent 设计一条**互不重复、各有侧重**的中文 query。用户目标中的主题必须体现在 query 中。"
        f"{_QUERY_CONCRETENESS_RULE}\n"
        "**输出格式**：严格按 Agent 顺序，每行一条 query，共 N 行。不要编号、不要 Agent 名、不要解释，仅 query 正文。长度每条控制在 40 字以内。"
    )


def synthesize_collaboration_query(
    agents_list: list[str],
    queries_per_agent: list[str],
    *,
    fallback_query: str | None = None,
) -> str:
    """
    将每个 Agent 的独立 query 合成为一条兼容旧协作接口的总 query。

    第 1 阶段仍需兼容 agent_master_run_flow_once(query=...) 的单字符串入参，
    因此先把 per-agent 指令显式拼入总 query，避免多个 agent 收到完全同质的任务描述。
    """
    pairs: list[tuple[str, str]] = []
    for idx, agent in enumerate(agents_list):
        if not isinstance(agent, str) or not agent.strip():
            continue
        query = queries_per_agent[idx] if idx < len(queries_per_agent) else ""
        if isinstance(query, str) and query.strip():
            pairs.append((agent.strip(), query.strip()))

    if not pairs:
        return (fallback_query or "").strip()

    if len(pairs) == 1:
        return pairs[0][1]

    lines = [
        "请按以下分工协作完成同一份最终输出，各 Agent 只负责自己的部分，最后整合为一份完整结果：",
    ]
    for agent, query in pairs:
        lines.append(f"- {agent}: {query}")
    lines.append("要求：围绕同一主题协作，避免重复回答，最终形成一份整合后的统一结果。")
    return "\n".join(lines).strip()


def suggest_queries_for_agents(
    task_spec: TaskSpec,
    agent_descriptor: str,
    agents_list: list[str],
    *,
    scenario_context: str | None = None,
    goal: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[str]:
    """
    多 Agent 时：根据任务描述与 Agent 列表，用 LLM 为每个 Agent 生成一条 query，与 scenario 联动。
    返回与 agents_list 顺序一致的 query 列表；若 LLM 不可用或返回行数不足，用 fallback 补齐。
    """
    if not agents_list or len(agents_list) < 2:
        return []
    fallback = ""
    if task_spec.initial_state and isinstance(task_spec.initial_state, dict):
        fallback = (task_spec.initial_state.get("query") or "").strip()
    if not fallback:
        fallback = "简要介绍你自己"

    if OpenAI is None:
        return [fallback] * len(agents_list)
    _key = resolve_api_key(api_key, provider)
    if not _key:
        return [fallback] * len(agents_list)

    _prov, _model, _base = _resolve_suggester_llm(provider, model, base_url)

    try:
        prompt = _build_prompt_multi_agent(
            task_spec,
            agent_descriptor,
            agents_list,
            scenario_context=scenario_context,
            goal=goal,
        )
        resp = chat_completion_with_retry(
            model=_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            timeout=60.0,
            provider=_prov,
            api_key=_key,
            base_url=_base,
            max_retries=3,
            timing_label="query_multi_agent",
        )
        content = (resp.choices[0].message.content or "").strip()
        lines = [ln.strip() for ln in content.split("\n") if ln.strip()]
        # 去掉行首编号（如 "1. "）
        queries = []
        for ln in lines:
            q = re.sub(r"^\s*\d+[\.．]\s*", "", ln).strip()
            q = re.sub(r'^["\']|["\']$', "", q)
            if q:
                queries.append(q[:120])
        if len(queries) >= len(agents_list):
            return queries[: len(agents_list)]
        # 不足则用 fallback 补齐
        while len(queries) < len(agents_list):
            queries.append(fallback)
        return queries
    except Exception as e:
        import warnings
        warnings.warn(
            f"Query 建议器（多 Agent）LLM 调用失败，使用回退 query: {e}",
            stacklevel=2,
        )
        return [fallback] * len(agents_list)


def _parse_query_and_rationale(text: str) -> tuple[str, str | None]:
    """从「两行」回复中解析：第一行 = query，第二行以「选择思路：」开头则取其后为 rationale。"""
    lines = [ln.strip() for ln in (text or "").strip().split("\n") if ln.strip()]
    query = ""
    rationale = None
    if lines:
        query = re.sub(r'^["\']|["\']$', "", lines[0]).strip()
    for ln in lines[1:]:
        if ln.startswith("选择思路：") or ln.startswith("选择思路:"):
            rationale = ln.split("：", 1)[-1].split(":", 1)[-1].strip()
            break
    return (query or "", rationale if (rationale and rationale.strip()) else None)


def suggest_query(
    task_spec: TaskSpec,
    agent_descriptor: str,
    *,
    scenario_context: str | None = None,
    goal: str | None = None,
    previous_queries: list[str] | None = None,
    previous_rounds: list[dict[str, Any]] | None = None,
    multi_round_strategy: MultiRoundStrategy = "auto",
    policy_hint: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    provider: str | None = None,
) -> str:
    """
    根据 TaskSpec 与待测 Agent 描述，调用 LLM 生成一条测试 query。
    多轮时优先传入 previous_rounds（每轮含 query、success、step_count、details、llm_judge）；
    policy_hint 为规则策略给出的具体出题要求，若提供则覆盖 strategy 的默认说明。
    根据「上一轮问什么 + 表现如何」决定下一轮问什么、怎么问；仅传 previous_queries 时则仅基于历史 query 做深化/换领域。
    返回非空字符串；若 LLM 不可用或失败，则回退到 task_spec.initial_state.query 或默认句。
    provider 可选：grok（xAI）、qwen、或 None（OpenAI）。环境变量同 B2：OPENAI_API_KEY、XAI_API_KEY、OPENAI_API_BASE 等。
    """
    fallback = ""
    if task_spec.initial_state and isinstance(task_spec.initial_state, dict):
        fallback = (task_spec.initial_state.get("query") or "").strip()
    if not fallback:
        fallback = "简要介绍你自己"

    if OpenAI is None:
        import warnings
        warnings.warn("Query 建议器：未安装 openai，使用回退 query。pip install openai 后可用 LLM 出题。", stacklevel=2)
        return fallback
    _key = resolve_api_key(api_key, provider)
    if not _key:
        import warnings
        warnings.warn("Query 建议器：未设置 API Key，使用回退 query。请在 .env 中配置对应 provider 的 Key。", stacklevel=2)
        return fallback

    _prov, _model, _base = _resolve_suggester_llm(provider, model, base_url)

    try:
        if previous_rounds:
            prompt = _build_prompt_with_performance(
                task_spec, agent_descriptor, previous_rounds, multi_round_strategy,
                policy_hint=policy_hint,
                scenario_context=scenario_context,
                goal=goal,
            )
        elif previous_queries:
            prompt = _build_prompt_multi_round(
                task_spec,
                agent_descriptor,
                previous_queries,
                multi_round_strategy,
                scenario_context=scenario_context,
                goal=goal,
            )
        else:
            prompt = _build_prompt(task_spec, agent_descriptor, scenario_context=scenario_context, goal=goal)
        resp = chat_completion_with_retry(
            model=_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            timeout=30.0,
            provider=_prov,
            api_key=_key,
            base_url=_base,
            max_retries=3,
            timing_label="query",
        )
        content = (resp.choices[0].message.content or "").strip()
        query = _parse_query(content)
        return query if query else fallback
    except Exception as e:
        import warnings
        warnings.warn(f"Query 建议器 LLM 调用失败，使用回退 query: {e}", stacklevel=2)
        return fallback


def suggest_query_with_rationale(
    task_spec: TaskSpec,
    agent_descriptor: str,
    *,
    scenario_context: str | None = None,
    goal: str | None = None,
    previous_queries: list[str] | None = None,
    previous_rounds: list[dict[str, Any]] | None = None,
    multi_round_strategy: MultiRoundStrategy = "auto",
    policy_hint: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    provider: str | None = None,
) -> tuple[str, str | None]:
    """
    与 suggest_query 相同，但多轮时（有 previous_rounds 或 previous_queries）会请 LLM 同时输出「选择思路」，
    返回 (query, rationale)；首轮或无历史时返回 (query, None)。policy_hint 为规则策略给出的出题要求。
    """
    fallback = ""
    if task_spec.initial_state and isinstance(task_spec.initial_state, dict):
        fallback = (task_spec.initial_state.get("query") or "").strip()
    if not fallback:
        fallback = "简要介绍你自己"

    if not previous_rounds and not previous_queries:
        return (suggest_query(
            task_spec, agent_descriptor,
            scenario_context=scenario_context,
            goal=goal,
            previous_queries=previous_queries,
            previous_rounds=previous_rounds,
            multi_round_strategy=multi_round_strategy,
            policy_hint=policy_hint,
            model=model, api_key=api_key, base_url=base_url, provider=provider,
        ), None)

    if OpenAI is None:
        import warnings
        warnings.warn("Query 建议器：未安装 openai，使用回退 query。", stacklevel=2)
        return (fallback, None)
    _key = resolve_api_key(api_key, provider)
    if not _key:
        import warnings
        warnings.warn("Query 建议器：未设置 API Key，使用回退 query。", stacklevel=2)
        return (fallback, None)

    _prov, _model, _base = _resolve_suggester_llm(provider, model, base_url)

    try:
        if previous_rounds:
            prompt = _build_prompt_with_performance(
                task_spec, agent_descriptor, previous_rounds, multi_round_strategy,
                policy_hint=policy_hint,
                scenario_context=scenario_context,
                goal=goal,
            ) + _RATIONALE_PROMPT_SUFFIX
        else:
            prompt = _build_prompt_multi_round(
                task_spec,
                agent_descriptor,
                previous_queries or [],
                multi_round_strategy,
                scenario_context=scenario_context,
                goal=goal,
            ) + _RATIONALE_PROMPT_SUFFIX
        resp = chat_completion_with_retry(
            model=_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            timeout=30.0,
            provider=_prov,
            api_key=_key,
            base_url=_base,
            max_retries=3,
            timing_label="query_rationale",
        )
        content = (resp.choices[0].message.content or "").strip()
        query, rationale = _parse_query_and_rationale(content)
        return (query if query else fallback, rationale)
    except Exception as e:
        import warnings
        warnings.warn(f"Query 建议器 LLM 调用失败，使用回退 query: {e}", stacklevel=2)
        return (fallback, None)
