"""B7 真实 RPA 适配器：基于 Vibium（AI 原生浏览器自动化），返回统一 ExecutionResult。"""
from raft.contracts.models import ExecutionResult, ToolCall

try:
    from vibium import browser_sync
except ImportError:
    browser_sync = None  # type: ignore


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


class VibiumRPA:
    """
    真实 RPA 适配器：使用 Vibium（AI 原生浏览器自动化）执行操作，返回统一 ExecutionResult。
    需安装：pip install vibium
    异常在内部捕获并归一化为 error_type + message。
    """

    def __init__(
        self,
        *,
        base_url: str = "about:blank",
        headless: bool = True,
        timeout_ms: int = 30_000,
    ) -> None:
        if browser_sync is None:
            raise ImportError(
                "Vibium not installed. Install with: pip install vibium "
                "(or pip install raft[vibium])"
            )
        self.base_url = base_url
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._vibe = None

    def _ensure_vibe(self):
        """延迟初始化 Vibium 浏览器实例。"""
        if self._vibe is not None:
            return self._vibe
        if browser_sync is None:
            raise ImportError("Vibium not installed. pip install vibium")
        self._vibe = browser_sync.launch()
        return self._vibe

    def execute(self, step_index: int, tool_call: ToolCall) -> ExecutionResult:
        """执行一次工具调用，返回统一 ExecutionResult。"""
        try:
            vibe = self._ensure_vibe()
            name = tool_call.tool_name
            params = tool_call.params or {}

            if name == "open_system":
                url = params.get("url") or params.get("target") or self.base_url
                if url == "demo":
                    url = self.base_url
                vibe.go(url)
                return _to_execution_result(
                    True,
                    raw_response={"url": url},
                    ui_state_delta={"url": url},
                    tool_name=name,
                )

            if name == "fetch_details":
                png = vibe.screenshot()
                return _to_execution_result(
                    True,
                    raw_response={"step": step_index, "screenshot_size": len(png) if png else 0},
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

            if name == "fill_form":
                selector = params.get("selector", "input")
                value = params.get("value", "")
                el = vibe.find(selector)
                # Vibium 元素 API：.type(text) 或 .send_keys(text)，视版本而定
                if hasattr(el, "type"):
                    el.type(value)
                elif hasattr(el, "send_keys"):
                    el.send_keys(value)
                elif hasattr(vibe, "type"):
                    vibe.type(selector, value)
                else:
                    raise NotImplementedError(
                        "Vibium fill_form: 当前版本未提供 type/send_keys，请升级 vibium 或反馈"
                    )
                return _to_execution_result(
                    True,
                    raw_response={"filled": selector},
                    ui_state_delta={"filled": selector},
                    tool_name=name,
                )

            if name == "click":
                selector = params.get("selector", "button")
                el = vibe.find(selector)
                el.click()
                return _to_execution_result(
                    True,
                    raw_response={"clicked": selector},
                    ui_state_delta={"clicked": selector},
                    tool_name=name,
                )

            return _to_execution_result(
                True,
                raw_response={"note": f"tool {name} no-op in VibiumRPA"},
                tool_name=name,
            )

        except Exception as e:
            err_type, msg = _norm_error(e)
            return _to_execution_result(
                False, error_type=err_type, raw_response=msg, tool_name=tool_call.tool_name
            )

    def close(self) -> None:
        """释放浏览器资源。"""
        if self._vibe is not None:
            try:
                self._vibe.quit()
            except Exception:
                pass
            self._vibe = None
