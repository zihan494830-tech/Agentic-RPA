"""
Poffices RPA 流程块：通用语义 Block（app_ready / send_query / get_response）与
Poffices 别名（poffices_bootstrap / poffices_query），供 BlockRegistry 按 block_id 执行。
"""
from typing import Any

from raft.contracts.models import ExecutionResult
from raft.core.query_suggester import synthesize_collaboration_query
from raft.rpa.blocks import BlockRegistry, get_default_block_registry


def _agent_name_from_params(params: dict[str, Any]) -> str | None:
    """从 params.options.agent_name 读取要选的 Agent；未指定时返回 None（不切换 Agent）。"""
    opts = params.get("options") if isinstance(params.get("options"), dict) else {}
    name = opts.get("agent_name")
    return (name.strip() if isinstance(name, str) and name else None) or None


def _get_page(rpa: Any) -> Any:
    if hasattr(rpa, "get_page"):
        return rpa.get_page()
    return rpa._ensure_page()


def _get_timeout_ms(rpa: Any) -> int:
    if hasattr(rpa, "get_timeout_ms"):
        return int(rpa.get_timeout_ms())
    return int(getattr(rpa, "timeout_ms", 30_000))


def _get_query_wait_sec(rpa: Any, *, minimum: int = 60) -> int:
    if hasattr(rpa, "get_query_wait_sec"):
        return int(rpa.get_query_wait_sec(minimum=minimum))
    return max(int(getattr(rpa, "query_wait_sec", 300)), minimum)


def _consume_fault_get_response(rpa: Any) -> bool:
    """故障注入：若 rpa.fault_get_response_remaining > 0，消耗一次并返回 True（应强制 timeout）。"""
    remaining = getattr(rpa, "fault_get_response_remaining", 0)
    if remaining > 0:
        setattr(rpa, "fault_get_response_remaining", remaining - 1)
        return True
    return False


def _get_credentials(rpa: Any) -> tuple[str | None, str | None]:
    if hasattr(rpa, "get_credentials"):
        username, password = rpa.get_credentials()
        return username, password
    return getattr(rpa, "_username", None), getattr(rpa, "_password", None)


def _is_followup_query(rpa: Any) -> bool:
    if hasattr(rpa, "is_followup_query"):
        return bool(rpa.is_followup_query())
    return bool(getattr(rpa, "_has_completed_first_query", False))


def _mark_query_completed(rpa: Any) -> None:
    if hasattr(rpa, "mark_query_completed"):
        rpa.mark_query_completed()
        return
    if hasattr(rpa, "_has_completed_first_query"):
        rpa._has_completed_first_query = True


def _result(
    success: bool,
    *,
    error_type: str | None = None,
    raw_response: str | dict[str, Any] | None = None,
    ui_state_delta: dict[str, Any] | None = None,
    tool_name: str | None = None,
) -> ExecutionResult:
    output_text = None
    if isinstance(raw_response, str) and raw_response.strip():
        output_text = raw_response.strip()
    elif isinstance(raw_response, dict):
        for key in ("response", "final_report", "text", "output", "message"):
            value = raw_response.get(key)
            if isinstance(value, str) and value.strip():
                output_text = value.strip()
                break
    if output_text is None and isinstance(ui_state_delta, dict):
        for key in ("response_text", "poffices_response", "final_report", "text"):
            value = ui_state_delta.get(key)
            if isinstance(value, str) and value.strip():
                output_text = value.strip()
                break
    extra: dict[str, Any] = {}
    if isinstance(raw_response, dict):
        imgs = raw_response.get("images")
        lnk = raw_response.get("links")
        if isinstance(imgs, list) or isinstance(lnk, list):
            extra["poffices_capture"] = {
                "images": imgs if isinstance(imgs, list) else [],
                "links": lnk if isinstance(lnk, list) else [],
            }
    return ExecutionResult(
        success=success,
        error_type=error_type,
        raw_response=raw_response,
        output_text=output_text,
        ui_state_delta=ui_state_delta,
        tool_name=tool_name,
        extra=extra,
    )


class AppReadyBlock:
    """通用 Block：打开应用并进入可操作状态（登录、选 Agent、Apply）；多轮时点 New question。"""

    def __init__(self, block_id: str = "app_ready") -> None:
        self._block_id = block_id

    @property
    def block_id(self) -> str:
        return self._block_id

    def run(
        self,
        *,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> ExecutionResult:
        rpa = context.get("rpa")
        if rpa is None:
            return _result(
                False,
                error_type="missing_context",
                raw_response="context['rpa'] 缺失，需传入 PofficesRPA 实例",
                tool_name=self._block_id,
            )
        try:
            from raft.rpa.poffices_bootstrap import (
                click_new_question,
                run_bootstrap_on_page,
                select_agent_on_current_page,
            )

            page = _get_page(rpa)
            timeout_ms = _get_timeout_ms(rpa)
            agent_name = _agent_name_from_params(params or {})

            if _is_followup_query(rpa):
                # "New question" 按钮只有在上一次文档生成完成后才出现；
                # 生成约需 2 分钟，click_new_question 的超时须大于此值，
                # 否则若上一轮仍在生成中则会触发不必要的 timeout 重试。
                new_q_timeout_ms = max(timeout_ms, 180_000)
                click_new_question(page, timeout_ms=new_q_timeout_ms)
                if agent_name:
                    page.wait_for_timeout(2000)  # 等待 New question 后页面/侧栏稳定再切 Agent
                    select_agent_on_current_page(page, agent_name, timeout_ms=timeout_ms)
                return _result(
                    True,
                    raw_response={"message": "app_ready (new_question)" + (" + switch_agent" if agent_name else "") + " done"},
                    ui_state_delta={"poffices_ready": True, "app_ready": True},
                    tool_name=self._block_id,
                )
            resume = bool(getattr(rpa, "_resume_bootstrap_on_current_page", False))
            bootstrap_kw: dict[str, Any] = {}
            if agent_name is not None:
                bootstrap_kw["agent_name"] = agent_name
            run_bootstrap_on_page(
                page,
                username=_get_credentials(rpa)[0] or None,
                password=_get_credentials(rpa)[1] or None,
                timeout_ms=timeout_ms,
                resume_on_current_page=resume,
                **bootstrap_kw,
            )
            if resume:
                setattr(rpa, "_resume_bootstrap_on_current_page", False)
            return _result(
                True,
                raw_response={"message": "app_ready done", "agent_name": agent_name},
                ui_state_delta={"poffices_ready": True, "app_ready": True},
                tool_name=self._block_id,
            )
        except Exception as e:
            etype = "timeout" if "Timeout" in str(type(e).__name__) else "rpa_execution_failed"
            return _result(
                False,
                error_type=etype,
                raw_response=str(e),
                tool_name=self._block_id,
            )


class SendQueryBlock:
    """通用 Block：在已就绪的会话中发送一条查询并触发执行（仅填框+发送，不等待结果）。"""

    @property
    def block_id(self) -> str:
        return "send_query"

    def run(
        self,
        *,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> ExecutionResult:
        rpa = context.get("rpa")
        if rpa is None:
            return _result(
                False,
                error_type="missing_context",
                raw_response="context['rpa'] 缺失",
                tool_name=self.block_id,
            )
        try:
            from raft.rpa.poffices_bootstrap import fill_query_and_send

            page = _get_page(rpa)
            query = (params or {}).get("query")
            if not isinstance(query, str) or not query.strip():
                return _result(
                    False,
                    error_type="validation_error",
                    raw_response="参数 query 缺失或为空",
                    tool_name=self.block_id,
                )
            query = query.strip()
            timeout_ms = _get_timeout_ms(rpa)
            fill_query_and_send(page, query, timeout_ms=timeout_ms)
            return _result(
                True,
                raw_response={"message": "send_query done", "query": query},
                ui_state_delta={"query_sent": True},
                tool_name=self.block_id,
            )
        except Exception as e:
            etype = "timeout" if "Timeout" in str(type(e).__name__) else "rpa_execution_failed"
            return _result(False, error_type=etype, raw_response=str(e), tool_name=self.block_id)


class GetResponseBlock:
    """通用 Block：等待当前任务完成并取回结果内容。"""

    @property
    def block_id(self) -> str:
        return "get_response"

    def run(
        self,
        *,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> ExecutionResult:
        rpa = context.get("rpa")
        if rpa is None:
            return _result(
                False,
                error_type="missing_context",
                raw_response="context['rpa'] 缺失",
                tool_name=self.block_id,
            )
        try:
            from raft.rpa.poffices_bootstrap import wait_and_capture_assets

            page = _get_page(rpa)
            if _consume_fault_get_response(rpa):
                raise TimeoutError("[故障注入] get_response 强制超时（fault_get_response_remaining 消耗）")
            query_wait_sec = _get_query_wait_sec(rpa, minimum=60)
            assets = wait_and_capture_assets(
                page,
                timeout_sec=query_wait_sec,
                check_interval_sec=2.0,
            )
            response_text = (assets.get("text") or "").strip()
            images = assets.get("images") if isinstance(assets.get("images"), list) else []
            links = assets.get("links") if isinstance(assets.get("links"), list) else []
            _mark_query_completed(rpa)
            return _result(
                True,
                raw_response={
                    "response": response_text,
                    "text": response_text,
                    "images": images,
                    "links": links,
                },
                ui_state_delta={
                    "poffices_response": response_text,
                    "response_text": response_text,
                    "poffices_assets": {"text": response_text, "images": images, "links": links},
                },
                tool_name=self.block_id,
            )
        except Exception as e:
            etype = "timeout" if "Timeout" in str(type(e).__name__) else "rpa_execution_failed"
            return _result(False, error_type=etype, raw_response=str(e), tool_name=self.block_id)


class WaitOutputCompleteBlock:
    """独立 Block：仅等待页面出现「生成完毕」标识（如绿色 Toast），不提取内容。
    用于规划/重试时单独重试「等待完成」而不重做 send_query 或整段 get_response。"""

    @property
    def block_id(self) -> str:
        return "wait_output_complete"

    def run(
        self,
        *,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> ExecutionResult:
        rpa = context.get("rpa")
        if rpa is None:
            return _result(
                False,
                error_type="missing_context",
                raw_response="context['rpa'] 缺失",
                tool_name=self.block_id,
            )
        try:
            from raft.rpa.poffices_bootstrap import wait_for_generation_complete

            page = _get_page(rpa)
            query_wait_sec = _get_query_wait_sec(rpa, minimum=60)
            timeout_sec = (params or {}).get("timeout_sec")
            if isinstance(timeout_sec, (int, float)) and timeout_sec > 0:
                query_wait_sec = int(timeout_sec)
            wait_for_generation_complete(page, timeout_sec=query_wait_sec)
            return _result(
                True,
                raw_response={"message": "wait_output_complete done"},
                ui_state_delta={"generation_complete": True},
                tool_name=self.block_id,
            )
        except Exception as e:
            etype = "timeout" if "Timeout" in str(type(e).__name__) else "rpa_execution_failed"
            return _result(False, error_type=etype, raw_response=str(e), tool_name=self.block_id)


class RefreshPageBlock:
    """独立 Block：刷新当前页面，用于长时间卡顿（如 Loading 不消失）后重试。
    执行 page.reload，等待页面加载稳定后返回；恢复计划可在此后接 wait_output_complete + get_response 重新执行失败环节。"""

    @property
    def block_id(self) -> str:
        return "refresh_page"

    def run(
        self,
        *,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> ExecutionResult:
        rpa = context.get("rpa")
        if rpa is None:
            return _result(
                False,
                error_type="missing_context",
                raw_response="context['rpa'] 缺失",
                tool_name=self.block_id,
            )
        try:
            page = _get_page(rpa)
            timeout_ms = _get_timeout_ms(rpa)
            page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2500)  # 等待 SPA/接口稳定，再执行后续 wait_output_complete 等
            return _result(
                True,
                raw_response={"message": "refresh_page done"},
                ui_state_delta={"page_refreshed": True},
                tool_name=self.block_id,
            )
        except Exception as e:
            etype = "timeout" if "Timeout" in str(type(e).__name__) else "rpa_execution_failed"
            return _result(False, error_type=etype, raw_response=str(e), tool_name=self.block_id)


class PofficesBootstrapBlock:
    """Block：Poffices 登录/选 Agent/Apply（兼容旧名 poffices_bootstrap）；支持 options.agent_name。"""

    @property
    def block_id(self) -> str:
        return "poffices_bootstrap"

    def run(
        self,
        *,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> ExecutionResult:
        return AppReadyBlock(block_id="poffices_bootstrap").run(params=params, context=context)


class PofficesQueryBlock:
    """Block：在 Poffices 页面填写 query、等待生成完成并提取响应。"""

    @property
    def block_id(self) -> str:
        return "poffices_query"

    def run(
        self,
        *,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> ExecutionResult:
        rpa = context.get("rpa")
        if rpa is None:
            return _result(
                False,
                error_type="missing_context",
                raw_response="context['rpa'] 缺失，需传入 PofficesRPA 实例",
                tool_name=self.block_id,
            )
        try:
            from raft.rpa.poffices_bootstrap import fill_query_and_send, wait_and_capture_assets

            page = _get_page(rpa)
            query = (params or {}).get("query")
            if not isinstance(query, str) or not query.strip():
                return _result(
                    False,
                    error_type="validation_error",
                    raw_response="参数 query 缺失或为空字符串",
                    tool_name=self.block_id,
                )
            query = query.strip()
            timeout_ms = _get_timeout_ms(rpa)
            query_wait_sec = _get_query_wait_sec(rpa, minimum=60)

            fill_query_and_send(page, query, timeout_ms=timeout_ms)
            assets = wait_and_capture_assets(
                page,
                timeout_sec=query_wait_sec,
                check_interval_sec=2.0,
            )
            response_text = (assets.get("text") or "").strip()
            images = assets.get("images") if isinstance(assets.get("images"), list) else []
            links = assets.get("links") if isinstance(assets.get("links"), list) else []
            _mark_query_completed(rpa)

            return _result(
                True,
                raw_response={
                    "query": query,
                    "response": response_text,
                    "text": response_text,
                    "images": images,
                    "links": links,
                },
                ui_state_delta={
                    "poffices_response": response_text,
                    "poffices_assets": {"text": response_text, "images": images, "links": links},
                },
                tool_name=self.block_id,
            )
        except Exception as e:
            etype = "timeout" if "Timeout" in str(type(e).__name__) else "rpa_execution_failed"
            return _result(
                False,
                error_type=etype,
                raw_response=str(e),
                tool_name=self.block_id,
            )


class DiscoveryBootstrapBlock:
    """Block：最小化 Bootstrap，打开 Agent Master 面板供 Discovery 使用，不选 Agent。"""

    @property
    def block_id(self) -> str:
        return "discovery_bootstrap"

    def run(
        self,
        *,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> ExecutionResult:
        rpa = context.get("rpa")
        if rpa is None:
            return _result(
                False,
                error_type="missing_context",
                raw_response="context['rpa'] 缺失",
                tool_name=self.block_id,
            )
        try:
            from raft.rpa.poffices_bootstrap import ensure_agent_master_panel_visible

            page = _get_page(rpa)
            timeout_ms = _get_timeout_ms(rpa)
            ensure_agent_master_panel_visible(
                page,
                username=_get_credentials(rpa)[0],
                password=_get_credentials(rpa)[1],
                timeout_ms=timeout_ms,
            )
            return _result(
                True,
                raw_response={"message": "discovery_bootstrap done"},
                ui_state_delta={"discovery_ready": True},
                tool_name=self.block_id,
            )
        except Exception as e:
            return _result(
                False,
                error_type="rpa_execution_failed",
                raw_response=str(e),
                tool_name=self.block_id,
            )


class ListOfficesBlock:
    """Block：从 Agent Master 左侧面板抓取所有 office 名称。"""

    @property
    def block_id(self) -> str:
        return "list_offices"

    def run(
        self,
        *,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> ExecutionResult:
        rpa = context.get("rpa")
        if rpa is None:
            return _result(False, error_type="missing_context", raw_response="context['rpa'] 缺失", tool_name=self.block_id)
        try:
            from raft.rpa.poffices_bootstrap import list_offices

            page = _get_page(rpa)
            offices = list_offices(page, timeout_ms=_get_timeout_ms(rpa))
            return _result(
                True,
                raw_response={"offices": offices},
                ui_state_delta={"discovered_offices": offices},
                tool_name=self.block_id,
            )
        except Exception as e:
            return _result(False, error_type="rpa_execution_failed", raw_response=str(e), tool_name=self.block_id)


class ExpandOfficeBlock:
    """Block：点击展开指定 office。"""

    @property
    def block_id(self) -> str:
        return "expand_office"

    def run(
        self,
        *,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> ExecutionResult:
        rpa = context.get("rpa")
        if rpa is None:
            return _result(False, error_type="missing_context", raw_response="context['rpa'] 缺失", tool_name=self.block_id)
        office_name = (params or {}).get("office_name") or (params or {}).get("office")
        if not (office_name and str(office_name).strip()):
            return _result(
                False,
                error_type="validation_error",
                raw_response="参数 office_name 缺失",
                tool_name=self.block_id,
            )
        try:
            from raft.rpa.poffices_bootstrap import expand_office

            page = _get_page(rpa)
            ok = expand_office(page, str(office_name).strip(), timeout_ms=_get_timeout_ms(rpa))
            return _result(
                ok,
                raw_response={"office_expanded": ok, "office_name": str(office_name).strip()},
                ui_state_delta={"office_expanded": str(office_name).strip() if ok else None},
                tool_name=self.block_id,
            )
        except Exception as e:
            return _result(False, error_type="rpa_execution_failed", raw_response=str(e), tool_name=self.block_id)


class ListAgentsInOfficeBlock:
    """Block：从当前展开的 office 抓取 agent 名称列表。"""

    @property
    def block_id(self) -> str:
        return "list_agents_in_office"

    def run(
        self,
        *,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> ExecutionResult:
        rpa = context.get("rpa")
        if rpa is None:
            return _result(False, error_type="missing_context", raw_response="context['rpa'] 缺失", tool_name=self.block_id)
        try:
            from raft.rpa.poffices_bootstrap import list_agents_in_office

            page = _get_page(rpa)
            raw = params or {}
            office_name = raw.get("office_name") or raw.get("office")
            if isinstance(office_name, str):
                office_name = office_name.strip() or None
            else:
                office_name = None
            agents = list_agents_in_office(
                page,
                office_name,
                timeout_ms=_get_timeout_ms(rpa),
            )
            return _result(
                True,
                raw_response={"agents": agents},
                ui_state_delta={"discovered_agents": agents},
                tool_name=self.block_id,
            )
        except Exception as e:
            return _result(False, error_type="rpa_execution_failed", raw_response=str(e), tool_name=self.block_id)


class AgentMasterSelectAgentsForFlowBlock:
    """
    Block：清空右侧 Selected Agents，按顺序添加指定 Agent 列表，Apply。
    用于 Agent Master 协作流程：先 Clear All，再按 agents 顺序 add，顺序即 Step1/2/3...
    """

    @property
    def block_id(self) -> str:
        return "agent_master_select_agents_for_flow"

    def run(
        self,
        *,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> ExecutionResult:
        rpa = context.get("rpa")
        if rpa is None:
            return _result(
                False,
                error_type="missing_context",
                raw_response="context['rpa'] 缺失",
                tool_name=self.block_id,
            )
        agents = params.get("agents") if isinstance(params, dict) else None
        if not isinstance(agents, list) or len(agents) == 0:
            return _result(
                False,
                error_type="validation_error",
                raw_response="参数 agents 缺失或为空列表",
                tool_name=self.block_id,
            )
        agents = [str(a).strip() for a in agents if a and str(a).strip()]
        if not agents:
            return _result(
                False,
                error_type="validation_error",
                raw_response="参数 agents 无有效 Agent 名",
                tool_name=self.block_id,
            )
        try:
            from raft.rpa.poffices_bootstrap import (
                _ensure_agent_master_mode_on,
                _is_apply_needed,
                add_agent_to_flow,
                clear_selected_agents,
                ensure_agent_master_panel_visible,
            )

            page = _get_page(rpa)
            timeout_ms = _get_timeout_ms(rpa)

            # 若页面不在 Agent Master 面板（"Enable Agent Master Mode" 不可见），
            # 先导航过去，确保后续操作在正确的页面状态下执行。
            _toggle_visible = page.get_by_text("Enable Agent Master Mode").first.is_visible()
            if not _toggle_visible:
                ensure_agent_master_panel_visible(
                    page,
                    username=_get_credentials(rpa)[0],
                    password=_get_credentials(rpa)[1],
                    timeout_ms=timeout_ms,
                )
                page.wait_for_timeout(1000)

            clear_selected_agents(page, timeout_ms=timeout_ms)
            page.wait_for_timeout(500)

            added: list[str] = []
            for name in agents:
                if add_agent_to_flow(page, name, timeout_ms=timeout_ms):
                    added.append(name)
                page.wait_for_timeout(500)

            _ensure_agent_master_mode_on(page, timeout_ms=timeout_ms)
            page.wait_for_timeout(1500)

            if _is_apply_needed(page):
                apply_btn = page.locator(
                    'button:has-text("Apply"), [role="button"]:has-text("Apply")'
                ).first
                apply_btn.wait_for(state="visible", timeout=timeout_ms)
                apply_btn.scroll_into_view_if_needed()
                apply_btn.click()
                page.wait_for_timeout(2000)

            return _result(
                True,
                raw_response={"agents_selected": added},
                ui_state_delta={"agent_master_flow_agents": added},
                tool_name=self.block_id,
            )
        except Exception as e:
            return _result(
                False,
                error_type="rpa_execution_failed",
                raw_response=str(e),
                tool_name=self.block_id,
            )


class AgentMasterRunFlowOnceBlock:
    """
    Block：在已配置好的 Agent Master Flow 下，输入 query，自动执行多步流程直到完成，提取最终报告。
    内部循环：fill_query_and_send → wait_for_generation_complete → has_next_step? click_next_step : break → extract_response
    """

    @property
    def block_id(self) -> str:
        return "agent_master_run_flow_once"

    def run(
        self,
        *,
        params: dict[str, Any],
        context: dict[str, Any],
    ) -> ExecutionResult:
        rpa = context.get("rpa")
        if rpa is None:
            return _result(
                False,
                error_type="missing_context",
                raw_response="context['rpa'] 缺失",
                tool_name=self.block_id,
            )
        raw_params = params or {}
        query = raw_params.get("query")
        queries = raw_params.get("queries") or raw_params.get("queries_per_agent")
        agents = raw_params.get("agents")
        if isinstance(queries, list) and isinstance(agents, list):
            normalized_agents = [str(a).strip() for a in agents if isinstance(a, str) and a.strip()]
            normalized_queries = [str(q).strip() for q in queries if isinstance(q, str) and q.strip()]
            synthesized = synthesize_collaboration_query(
                normalized_agents,
                normalized_queries,
                fallback_query=query if isinstance(query, str) else None,
            )
            if synthesized:
                query = synthesized

        if not isinstance(query, str) or not query.strip():
            return _result(
                False,
                error_type="validation_error",
                raw_response="参数 query 缺失或为空",
                tool_name=self.block_id,
            )
        query = query.strip()
        try:
            from raft.rpa.poffices_bootstrap import (
                click_next_step,
                fill_query_and_send,
                has_next_step,
                wait_and_capture_assets,
                wait_for_generation_complete,
            )

            page = _get_page(rpa)
            timeout_ms = _get_timeout_ms(rpa)
            query_wait_sec = _get_query_wait_sec(rpa, minimum=60)
            step_timeout = (params or {}).get("timeout_sec")
            if isinstance(step_timeout, (int, float)) and step_timeout > 0:
                query_wait_sec = int(step_timeout)

            fill_query_and_send(page, query, timeout_ms=timeout_ms)

            max_steps = 10
            for _ in range(max_steps):
                wait_for_generation_complete(page, timeout_sec=query_wait_sec)
                if has_next_step(page, timeout_ms=3000):
                    click_next_step(page, timeout_ms=timeout_ms)
                    page.wait_for_timeout(2000)
                else:
                    break

            assets = wait_and_capture_assets(
                page,
                timeout_sec=query_wait_sec,
                check_interval_sec=2.0,
            )
            response_text = (assets.get("text") or "").strip()
            images = assets.get("images") if isinstance(assets.get("images"), list) else []
            links = assets.get("links") if isinstance(assets.get("links"), list) else []
            _mark_query_completed(rpa)

            return _result(
                True,
                raw_response={
                    "query": query,
                    "queries": queries if isinstance(queries, list) else None,
                    "final_report": response_text,
                    "response": response_text,
                    "text": response_text,
                    "images": images,
                    "links": links,
                },
                ui_state_delta={
                    "final_report": response_text,
                    "poffices_response": response_text,
                    "poffices_assets": {"text": response_text, "images": images, "links": links},
                    "agent_master_query": query,
                    "agent_master_queries": queries if isinstance(queries, list) else None,
                },
                tool_name=self.block_id,
            )
        except Exception as e:
            return _result(
                False,
                error_type="rpa_execution_failed",
                raw_response=str(e),
                tool_name=self.block_id,
            )


def register_poffices_blocks(registry: BlockRegistry | None = None) -> None:
    """将通用 Block（app_ready/send_query/get_response/wait_output_complete）与兼容旧名（poffices_bootstrap/poffices_query）注册。"""
    reg = registry or get_default_block_registry()
    reg.register("discovery_bootstrap", DiscoveryBootstrapBlock())
    reg.register("list_offices", ListOfficesBlock())
    reg.register("expand_office", ExpandOfficeBlock())
    reg.register("list_agents_in_office", ListAgentsInOfficeBlock())
    reg.register("app_ready", AppReadyBlock("app_ready"))
    reg.register("send_query", SendQueryBlock())
    reg.register("get_response", GetResponseBlock())
    reg.register("wait_output_complete", WaitOutputCompleteBlock())
    reg.register("refresh_page", RefreshPageBlock())
    reg.register("poffices_bootstrap", PofficesBootstrapBlock())
    reg.register("poffices_query", PofficesQueryBlock())
    reg.register("agent_master_select_agents_for_flow", AgentMasterSelectAgentsForFlowBlock())
    reg.register("agent_master_run_flow_once", AgentMasterRunFlowOnceBlock())


# 模块加载时注册到默认注册表，便于 PofficesRPA 直接通过 registry.execute() 使用
register_poffices_blocks()
