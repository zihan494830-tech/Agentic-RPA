"""B7 真实 RPA 适配器：基于 Playwright，所有调用返回统一 ExecutionResult，异常归一化。"""
from raft.contracts.models import ExecutionResult, ToolCall

try:
    from playwright.sync_api import sync_playwright, Browser, Page, TimeoutError as PlaywrightTimeout
except ImportError:
    sync_playwright = None  # type: ignore
    Browser = None  # type: ignore
    Page = None  # type: ignore
    PlaywrightTimeout = Exception  # type: ignore


def _to_execution_result(
    success: bool,
    *,
    error_type: str | None = None,
    raw_response: str | dict | None = None,
    ui_state_delta: dict | None = None,
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
    return ExecutionResult(
        success=success,
        error_type=error_type,
        raw_response=raw_response,
        output_text=output_text,
        ui_state_delta=ui_state_delta,
        tool_name=tool_name,
    )


def _norm_error(error: BaseException) -> tuple[str, str]:
    """将异常归一化为 (error_type, message)。"""
    msg = str(error)
    if "timeout" in msg.lower() or "Timeout" in type(error).__name__:
        return ("timeout", msg)
    if "not found" in msg.lower() or "selector" in msg.lower():
        return ("element_not_found", msg)
    if "network" in msg.lower() or "net::" in msg.lower():
        return ("network_error", msg)
    return ("rpa_execution_failed", msg)


class PlaywrightRPA:
    """
    真实 RPA 适配器：使用 Playwright 执行浏览器操作，所有调用返回统一 ExecutionResult。
    异常在内部捕获并归一化为 error_type + message。
    """

    def __init__(
        self,
        *,
        base_url: str = "about:blank",
        headless: bool = True,
        timeout_ms: int = 30_000,
    ) -> None:
        if sync_playwright is None:
            raise ImportError("Playwright not installed. Install with: pip install playwright && playwright install")
        self.base_url = base_url
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._playwright = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    def _ensure_page(self) -> "Page":
        if self._page is not None:
            return self._page
        if sync_playwright is None:
            raise ImportError("Playwright not installed. Install with: pip install playwright && playwright install")
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._page = self._browser.new_page()
        self._page.set_default_timeout(self.timeout_ms)
        return self._page

    def execute(self, step_index: int, tool_call: ToolCall) -> ExecutionResult:
        """执行一次工具调用，返回统一 ExecutionResult。"""
        try:
            page = self._ensure_page()
            name = tool_call.tool_name
            params = tool_call.params or {}

            if name == "open_system":
                url = params.get("url") or params.get("target") or self.base_url
                if url == "demo":
                    url = self.base_url
                page.goto(url, timeout=self.timeout_ms)
                return _to_execution_result(
                    True,
                    raw_response={"url": url},
                    ui_state_delta={"url": page.url},
                    tool_name=name,
                )

            if name == "fetch_details":
                return _to_execution_result(
                    True,
                    raw_response={"step": step_index},
                    ui_state_delta={"screen": f"step_{step_index}"},
                    tool_name=name,
                )

            if name == "retry_operation":
                return _to_execution_result(
                    True,
                    raw_response={"retried": True, "reason": params.get("reason")},
                    ui_state_delta={"retry": True},
                    tool_name=name,
                )

            # 多 Agent 编排：planner/verifier 角色工具（无浏览器操作，仅记录成功）
            if name in ("plan_step", "plan_next"):
                return _to_execution_result(
                    True,
                    raw_response={"planned": True, "step": step_index},
                    ui_state_delta={"plan": name},
                    tool_name=name,
                )
            if name in ("verify_step", "verify_next"):
                return _to_execution_result(
                    True,
                    raw_response={"verified": True, "step": step_index},
                    ui_state_delta={"verify": name},
                    tool_name=name,
                )

            if name == "fill_form":
                selector = params.get("selector", "input")
                value = params.get("value", "")
                page.fill(selector, value, timeout=self.timeout_ms)
                return _to_execution_result(
                    True,
                    raw_response={"filled": selector},
                    ui_state_delta={"filled": selector},
                    tool_name=name,
                )

            if name == "click":
                selector = params.get("selector", "button")
                page.click(selector, timeout=self.timeout_ms)
                return _to_execution_result(
                    True,
                    raw_response={"clicked": selector},
                    ui_state_delta={"clicked": selector},
                    tool_name=name,
                )

            # 未知工具：返回成功但标记为未实现
            return _to_execution_result(
                True,
                raw_response={"note": f"tool {name} no-op in PlaywrightRPA"},
                tool_name=name,
            )

        except PlaywrightTimeout as e:
            err_type, msg = _norm_error(e)
            return _to_execution_result(False, error_type=err_type, raw_response=msg, tool_name=tool_call.tool_name)
        except Exception as e:
            err_type, msg = _norm_error(e)
            return _to_execution_result(False, error_type=err_type, raw_response=msg, tool_name=tool_call.tool_name)

    def close(self) -> None:
        """释放浏览器资源。"""
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None
        self._page = None
