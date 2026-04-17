"""
B7 RPA 适配器：在 Poffices 页面上执行 app_ready / poffices_bootstrap、send_query、get_response / poffices_query。
供 B6 决策组件（PofficesAgent 或 PofficesLLMAgent）驱动流程、测试 **待测 Agent**（Poffices 页面上的产品）时使用，与 raft.rpa.poffices_bootstrap 共用逻辑。
"""
import os
from typing import Any

from raft.contracts.models import ExecutionResult, ToolCall

try:
    from playwright.sync_api import sync_playwright, Browser, Page, TimeoutError as PlaywrightTimeout
except ImportError:
    sync_playwright = None  # type: ignore
    Browser = None  # type: ignore
    Page = None  # type: ignore
    PlaywrightTimeout = Exception  # type: ignore


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


class PofficesRPA:
    """
    专用于 Poffices 的 RPA：执行 poffices_bootstrap、poffices_query，
    返回统一 ExecutionResult，供闭环 1 回传给 PofficesAgent。
    """

    def __init__(
        self,
        *,
        headless: bool = False,
        timeout_ms: int = 30_000,
        username: str | None = None,
        password: str | None = None,
        query_wait_sec: int = 300,  # 等待绿色完成标识的最大秒数（实际按标识出现提前返回）
    ) -> None:
        if sync_playwright is None:
            raise ImportError(
                "Playwright not installed. Install with: pip install playwright && playwright install"
            )
        self._username = username or os.environ.get("POFFICES_USERNAME", "")
        self._password = password or os.environ.get("POFFICES_PASSWORD", "")
        if not self._username or not self._password:
            raise ValueError(
                "POFFICES_USERNAME / POFFICES_PASSWORD 未配置，请在 .env 中设置或通过参数传入。"
            )
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.query_wait_sec = query_wait_sec
        self.fault_get_response_remaining: int = 0  # 故障注入：剩余需要强制失败的 get_response 次数
        self._playwright = None
        self._browser: Browser | None = None
        self._context: Any = None
        self._page: Page | None = None
        self._has_completed_first_query: bool = False  # 多轮：首轮后改为点 New question
        # Discovery 后首轮 app_ready 跳过 page.goto，避免整页重载、延续同一 Agent Master 会话
        self._resume_bootstrap_on_current_page: bool = False

    # ---- Public API: 供 blocks 使用，避免直接依赖私有状态 -----------------

    def mark_resume_after_discovery(self) -> None:
        """由 run_poffices_agent 在 UI Discovery 成功后调用；下一轮 app_ready 不再 goto 站点根 URL。"""
        self._resume_bootstrap_on_current_page = True

    def get_page(self) -> "Page":
        return self._ensure_page()

    def get_timeout_ms(self) -> int:
        return self.timeout_ms

    def get_query_wait_sec(self, *, minimum: int = 60) -> int:
        return max(self.query_wait_sec, minimum)

    def get_credentials(self) -> tuple[str | None, str | None]:
        username = self._username or None
        password = self._password or None
        return username, password

    def is_followup_query(self) -> bool:
        return self._has_completed_first_query

    def mark_query_completed(self) -> None:
        self._has_completed_first_query = True

    def _ensure_page(self) -> "Page":
        if self._page is not None:
            return self._page
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)

        # 使用与真人浏览器一致的指纹，避免站点识别为自动化后返回空白页
        from raft.rpa.poffices_bootstrap import REAL_BROWSER_FINGERPRINT
        fp = REAL_BROWSER_FINGERPRINT
        self._context = self._browser.new_context(
            user_agent=fp["user_agent"],
            viewport=fp["viewport"],
            locale=fp.get("locale"),
            timezone_id=fp.get("timezone_id"),
        )
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)
        return self._page

    def execute(self, step_index: int, tool_call: ToolCall) -> ExecutionResult:
        # 优先走 BlockRegistry（RPA 流程块化）；无对应 block 时回退到下方原有分支
        import raft.rpa.poffices_blocks  # noqa: F401  # 确保 Poffices Block 已注册
        from raft.rpa.blocks import get_default_block_registry

        result = get_default_block_registry().execute(
            tool_call.tool_name,
            params=tool_call.params or {},
            context={"rpa": self, "step_index": step_index},
        )
        if result is not None:
            return result

        from raft.rpa.poffices_bootstrap import (
            run_bootstrap_on_page,
            fill_query_and_send,
            click_new_question,
            wait_and_capture_assets,
        )

        try:
            page = self._ensure_page()
            name = tool_call.tool_name
            params = tool_call.params or {}

            if name == "poffices_bootstrap":
                if self._has_completed_first_query:
                    # 多轮：点 New question
                    click_new_question(page, timeout_ms=self.timeout_ms)
                    return _result(
                        True,
                        raw_response={"message": "[fallback] poffices_new_question done"},
                        ui_state_delta={"poffices_ready": True},
                        tool_name=name,
                    )
                run_bootstrap_on_page(
                    page,
                    username=self._username or None,
                    password=self._password or None,
                    timeout_ms=self.timeout_ms,
                )
                return _result(
                    True,
                    raw_response={"message": "[fallback] poffices_bootstrap done"},
                    ui_state_delta={"poffices_ready": True},
                    tool_name=name,
                )

            if name == "poffices_query":
                query = params.get("query", "Hello")
                fill_query_and_send(page, query, timeout_ms=self.timeout_ms)
                assets = wait_and_capture_assets(
                    page,
                    timeout_sec=max(self.query_wait_sec, 60),
                    check_interval_sec=2.0,
                )
                response_text = (assets.get("text") or "").strip()
                images = assets.get("images") if isinstance(assets.get("images"), list) else []
                links = assets.get("links") if isinstance(assets.get("links"), list) else []
                self._has_completed_first_query = True
                return _result(
                    True,
                    raw_response={
                        "query": query,
                        "response": response_text,
                        "text": response_text,
                        "images": images,
                        "links": links,
                        "path": "fallback",
                    },
                    ui_state_delta={
                        "poffices_response": response_text,
                        "poffices_assets": {"text": response_text, "images": images, "links": links},
                    },
                    tool_name=name,
                )

            return _result(
                False,
                error_type="unknown_tool",
                raw_response=f"[fallback] PofficesRPA 不支持工具: {name}",
                tool_name=name,
            )

        except PlaywrightTimeout as e:
            return _result(
                False,
                error_type="timeout",
                raw_response=str(e),
                tool_name=tool_call.tool_name,
            )
        except Exception as e:
            return _result(
                False,
                error_type="rpa_execution_failed",
                raw_response=str(e),
                tool_name=tool_call.tool_name,
            )

    def close(self) -> None:
        if self._page and self._context:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None
