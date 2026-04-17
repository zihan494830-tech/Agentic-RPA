#!/usr/bin/env python
"""
Poffices.ai RPA Bootstrap：登录 → 启用 Agent Master 模式 → 选择 Market Analysis Agent → 应用。

完整流程：
  Step 0: 打开首页 https://b1s2.hkrnd.com/
  Step 1: 检测 Login 按钮 → 若可见则登录（填 Username/Password，提交）
  Step 2: 等待选项框 → 点击 Agent Master → 关闭 Welcome 弹窗（Got it!）
  Step 2.5: 点击 Business Office 展开（必须先展开才能选 Market Analysis，否则会卡死）
  Step 3: 点 Market Analysis 选上
  Step 4: 开启 Enable Agent Master Mode 开关
  Step 5: 点击最下面的 Apply 保存
  Step 6:（可选 --full / --query-test）在输入框填 Query → 点击 Generate → 等待响应 → 提取并保存

凭证通过环境变量配置，请勿将密码写入代码。
运行：
  python scripts/run_poffices_bootstrap.py              # 仅 Bootstrap
  python scripts/run_poffices_bootstrap.py --query-test # Bootstrap + Query 测试
  python scripts/run_poffices_bootstrap.py --query-test --query "你的问题"
"""
import argparse
import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

try:
    from dotenv import load_dotenv
    load_dotenv(_root / ".env")
except ImportError:
    pass

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("请安装 playwright: pip install playwright && playwright install")
    sys.exit(1)

POFFICES_URL = "https://b1s2.hkrnd.com/"


def _log(msg: str, icon: str = "ℹ") -> None:
    print(f"[{icon}] {msg}")


def _dismiss_welcome_popup(page) -> bool:
    """若有 Welcome to Agent Master 弹窗，点击 Got it! 关闭，避免阻挡后续流程。"""
    try:
        got_it = page.get_by_role("button", name="Got it!").or_(page.get_by_text("Got it!"))
        if got_it.first.is_visible(timeout=2000):
            got_it.first.click()
            _log("已关闭 Welcome to Agent Master 弹窗（Got it!）", "✓")
            page.wait_for_timeout(500)
            return True
    except Exception:
        pass
    try:
        skip_btn = page.get_by_text("Skip").first
        if skip_btn.is_visible(timeout=1000):
            skip_btn.click()
            _log("已关闭引导弹窗（Skip）", "✓")
            page.wait_for_timeout(500)
            return True
    except Exception:
        pass
    return False


def _enable_page_scroll(page) -> None:
    """注入样式启用页面滚轮滚动，便于完整浏览网页。"""
    try:
        page.evaluate("""
            () => {
                document.documentElement.style.overflow = 'auto';
                document.documentElement.style.overflowY = 'auto';
                document.body.style.overflow = 'auto';
                document.body.style.overflowY = 'auto';
            }
        """)
    except Exception:
        pass


def _scroll_page(page, direction: str = "down", amount: int = 300) -> None:
    """模拟滚轮滚动页面。direction: down/up, amount: 像素。"""
    try:
        delta = amount if direction == "down" else -amount
        page.mouse.wheel(0, delta)
    except Exception:
        try:
            page.evaluate(f"window.scrollBy(0, {delta})")
        except Exception:
            pass


def _is_market_analysis_already_selected(page) -> bool:
    """检测 Market Analysis Agent 是否已选中。"""
    try:
        return page.evaluate("""
            () => {
                const els = Array.from(document.querySelectorAll('*')).filter(el =>
                    el.textContent?.trim() === 'Market Analysis' && el.offsetParent !== null
                );
                for (const el of els) {
                    let p = el;
                    for (let i = 0; i < 8 && p; i++) {
                        if (p.getAttribute?.('aria-selected') === 'true' ||
                            p.getAttribute?.('data-selected') === 'true' ||
                            /selected|active|checked|chosen/i.test(p.className || '')) {
                            return true;
                        }
                        p = p.parentElement;
                    }
                }
                return false;
            }
        """)
    except Exception:
        return False


def _is_agent_master_mode_already_on(page) -> bool:
    """检测 Enable Agent Master Mode 开关是否已开启。
    只检查与 label 同一行右侧的 switch/checkbox 元素，避免误报（如 'inactive' 含 'active'）。
    """
    try:
        return page.evaluate("""
            () => {
                const label = Array.from(document.querySelectorAll('*')).find(el =>
                    el.textContent?.trim() === 'Enable Agent Master Mode' && el.offsetParent !== null
                );
                if (!label) return false;
                const labelRect = label.getBoundingClientRect();
                const isRightOfLabel = (el) => el.getBoundingClientRect().left >= labelRect.right - 10;
                const isChecked = (c) => {
                    const ac = c.getAttribute?.('aria-checked');
                    const ds = c.getAttribute?.('data-state');
                    if (ac === 'true' || ds === 'checked') return true;
                    return /\\b(checked|on)\\b/i.test(c.className || '');
                };
                let row = label.parentElement;
                for (let i = 0; i < 6 && row; i++) {
                    const strict = row.querySelectorAll('[role="switch"], [role="checkbox"], [data-state]');
                    for (const c of strict) {
                        if (isRightOfLabel(c) && isChecked(c)) return true;
                    }
                    const fallback = Array.from(row.querySelectorAll('*')).filter(isRightOfLabel);
                    for (const c of fallback) {
                        if (isChecked(c)) return true;
                    }
                    row = row.parentElement;
                }
                return false;
            }
        """)
    except Exception:
        return False


def _extract_market_analysis_agent_response(page, timeout_ms: int = 5000) -> str:
    """
    尝试从页面提取 Market Analysis Agent 的响应内容。
    使用多种常见选择器，若均失败则返回空字符串。
    """
    selectors = [
        '[class*="message"] [class*="content"]',
        '[class*="response"]',
        '[class*="output"]',
        '[class*="result"]',
        'article',
        '[class*="markdown"]',
        '.prose',
        '[role="article"]',
        'main [class*="content"]',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                # 取最后一个（通常是 AI 回复），排除过短内容
                for i in range(loc.count() - 1, -1, -1):
                    text = loc.nth(i).inner_text(timeout=min(2000, timeout_ms))
                    if text and len(text.strip()) > 20:
                        return text.strip()
        except Exception:
            continue
    return ""


# 模拟真实浏览器指纹，降低反爬/风控检测
REAL_BROWSER_FINGERPRINT = {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "viewport": {"width": 1920, "height": 1080},
    "locale": "zh-TW",
    "timezone_id": "Asia/Hong_Kong",
}
SLOW_MO_MS = 1000


def run_bootstrap(
    *,
    headless: bool = False,
    run_full: bool = False,
    query: str = "test",
    wait_response_sec: int = 120,
    timeout_ms: int = 15_000,
    slow_mo_ms: int = SLOW_MO_MS,
    stop_after_login: bool = False,
) -> bool:
    """执行 Poffices.ai 登录 + Agent 选择流程。run_full 时会在流程结束后生成测试报告（输入/输出/调用的 RPA）。"""
    username = os.environ.get("POFFICES_USERNAME", "")
    password = os.environ.get("POFFICES_PASSWORD", "")
    if not username or not password:
        _log(
            "请在 .env 或环境变量中设置 POFFICES_USERNAME 和 POFFICES_PASSWORD",
            "❌",
        )
        return False

    rpa_steps: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            slow_mo=slow_mo_ms,
        )
        context = browser.new_context(
            user_agent=REAL_BROWSER_FINGERPRINT["user_agent"],
            viewport=REAL_BROWSER_FINGERPRINT["viewport"],
            locale=REAL_BROWSER_FINGERPRINT["locale"],
            timezone_id=REAL_BROWSER_FINGERPRINT["timezone_id"],
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        _log("使用模拟真实浏览器环境（Chrome 指纹 + zh-TW + Asia/Hong_Kong + slowMo）", "ℹ")

        try:
            # Step 0: 打开首页
            _log("打开 Poffices.ai ...")
            page.goto(POFFICES_URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            page.wait_for_timeout(1500)
            rpa_steps.append("Step 0：打开 Poffices.ai 首页")

            # 启用页面滚轮滚动，便于完整浏览
            _enable_page_scroll(page)

            # Step 1: 检测未登录并执行登录
            # 多种方式检测 Login：button/a 含 "Login" 文本，或 role=button/link
            login_btn = page.locator('button:has-text("Login"), a:has-text("Login"), [role="button"]:has-text("Login"), [role="link"]:has-text("Login")').first
            try:
                login_btn.wait_for(state="visible", timeout=5000)
                need_login = True
            except Exception:
                need_login = False

            if need_login:
                _log("检测到未登录（Login 可见），执行登录...")
                user_input = page.get_by_placeholder("Username or Email").or_(
                    page.locator('input[name="username"], input[type="text"]').first
                )
                form_visible = user_input.is_visible()
                if not form_visible:
                    # 表单在弹窗内，需先点击 Login 打开
                    login_btn.click()
                    page.wait_for_timeout(1500)
                    user_input.wait_for(state="visible", timeout=timeout_ms)
                user_input.fill(username)
                page.get_by_placeholder("Password").or_(
                    page.locator('input[name="password"], input[type="password"]').first
                ).fill(password)
                submit_btn = page.get_by_role("button", name="Login").or_(
                    page.get_by_role("button", name="Log in")
                ).or_(page.locator('button[type="submit"]')).first
                submit_btn.click()
                # 登录后等待跳转与内容加载
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
                page.wait_for_timeout(2000)
                # 若仍停留在登录相关页，显式跳转到首页
                still_on_login = "login" in page.url.lower()
                try:
                    still_on_login = still_on_login or page.get_by_role("button", name="Login").is_visible()
                except Exception:
                    pass
                if still_on_login:
                    page.goto(POFFICES_URL, wait_until="networkidle", timeout=timeout_ms)
                    page.wait_for_timeout(2000)
                _enable_page_scroll(page)
                _log("登录完成", "✓")
                rpa_steps.append("Step 1：登录（若需）")
            else:
                _log("已登录或无需登录", "✓")
                rpa_steps.append("Step 1：已登录或无需登录")

            if stop_after_login:
                _log("已登录，--stop-after-login 模式：暂停以便查看页面，按 Enter 继续或关闭...", "ℹ")
                if not headless:
                    input()
                return True

            # 等待主内容区加载，若有 Welcome to Agent Master 弹窗则点 Got it! 关闭
            page.wait_for_timeout(2000)
            _dismiss_welcome_popup(page)

            # Step 2: 等待主内容区，若未在 Agent Master 则点击进入
            _log("[Step 2] 等待主页面选项（Private/Agent Master/Digital Assets）...")
            doc_input = page.get_by_placeholder("Describe the document to generate").or_(
                page.locator("textarea").first
            )
            doc_input.wait_for(state="visible", timeout=timeout_ms)
            page.wait_for_timeout(1000)
            # 若已可见设置面板（Poffices Agent Platform），说明已在 Agent Master，跳过点击
            try:
                poffices_visible = page.get_by_text("Poffices Agent Platform").first.is_visible(timeout=2000)
            except Exception:
                poffices_visible = False
            if not poffices_visible:
                agent_master_opt = page.get_by_text("Agent Master").first
                agent_master_opt.scroll_into_view_if_needed()
                agent_master_opt.wait_for(state="visible", timeout=timeout_ms)
                _log("[Step 2] 点击 Agent Master 选项...")
                agent_master_opt.click()
                page.wait_for_timeout(2000)
                rpa_steps.append("Step 2：进入 Agent Master 模式")
            else:
                _log("[Step 2] 已在 Agent Master 模式，跳过", "✓")
                rpa_steps.append("Step 2：已在 Agent Master，跳过")

            _dismiss_welcome_popup(page)
            _enable_page_scroll(page)

            # Step 2.5: 必须先展开 Business Office，否则选不了 Market Analysis（会卡死）
            _log("[Step 2.5] 点击 Business Office 展开...")
            business_office = page.get_by_text("Business Office").first
            business_office.wait_for(state="visible", timeout=timeout_ms)
            business_office.scroll_into_view_if_needed()
            business_office.click()
            page.wait_for_timeout(800)
            rpa_steps.append("Step 2.5：展开 Business Office")

            # Step 3: 若 Market Analysis 未选中，则点击选上
            if _is_market_analysis_already_selected(page):
                _log("[Step 3] Market Analysis 已选中，跳过", "✓")
                rpa_steps.append("Step 3：Market Analysis 已选中，跳过")
            else:
                _log("[Step 3] 点击 Market Analysis 选上...")
                market_analysis_opt = page.get_by_text("Market Analysis", exact=True).first
                market_analysis_opt.wait_for(state="visible", timeout=timeout_ms)
                market_analysis_opt.scroll_into_view_if_needed()
                market_analysis_opt.click()
                page.wait_for_timeout(1000)
                rpa_steps.append("Step 3：选择 Market Analysis Agent")

            # Step 4: 若 Enable Agent Master Mode 开关未开启，则点击开启，并验证实际状态
            if _is_agent_master_mode_already_on(page):
                _log("[Step 4] Enable Agent Master Mode 已开启，跳过", "✓")
                rpa_steps.append("Step 4：Enable Agent Master Mode 已开启，跳过")
            else:
                _log("[Step 4] 开启 Enable Agent Master Mode 开关...")
                enable_text = page.get_by_text("Enable Agent Master Mode").first
                enable_text.wait_for(state="visible", timeout=timeout_ms)
                enable_text.scroll_into_view_if_needed()
                page.wait_for_timeout(500)
                try:
                    page.evaluate("""
                        () => {
                            const label = Array.from(document.querySelectorAll('*')).find(el =>
                                el.textContent?.trim() === 'Enable Agent Master Mode' && el.offsetParent !== null
                            );
                            if (!label) return;
                            const labelRect = label.getBoundingClientRect();
                            let row = label.parentElement;
                            for (let i = 0; i < 6 && row; i++) {
                                const rightChildren = Array.from(row.children).filter(c => c.getBoundingClientRect().left > labelRect.right);
                                const rightChild = rightChildren[rightChildren.length - 1];
                                if (rightChild) {
                                    rightChild.click();
                                    return;
                                }
                                const last = row.lastElementChild;
                                if (last && last.getBoundingClientRect().left > labelRect.right) {
                                    last.click();
                                    return;
                                }
                                row = row.parentElement;
                            }
                            const el = document.elementFromPoint(labelRect.right + 80, labelRect.top + labelRect.height / 2);
                            if (el && el !== document.body) el.click();
                        }
                    """)
                except Exception:
                    box = enable_text.bounding_box()
                    if box:
                        page.mouse.click(box["x"] + box["width"] + 100, box["y"] + box["height"] / 2)
                page.wait_for_timeout(800)
                if _is_agent_master_mode_already_on(page):
                    _log("[Step 4] 已开启 Enable Agent Master Mode", "✓")
                    rpa_steps.append("Step 4：开启 Enable Agent Master Mode")
                else:
                    _log("[Step 4] 已点击开关但未检测到开启状态，可能需手动确认", "⚠")
                    rpa_steps.append("Step 4：已点击开关（未检测到开启状态）")
            page.wait_for_timeout(1500)

            # Step 5: 点击最下面的 Apply 保存
            _log("[Step 5] 点击 Apply 保存...")
            apply_btn = page.locator('button:has-text("Apply"), [role="button"]:has-text("Apply")').first
            apply_btn.wait_for(state="visible", timeout=timeout_ms)
            apply_btn.scroll_into_view_if_needed()
            _log("[Step 5] 点击 Apply...")
            apply_btn.click()
            page.wait_for_timeout(2000)
            rpa_steps.append("Step 5：Apply 保存")

            _log("RPA Bootstrap 完成：已登录并选择 Market Analysis Agent", "✓")

            if run_full:
                # Step 6: 输入 Query 并点击发送按钮（右下角橙色箭头）→ 等待响应 → 提取并保存
                _log(f"输入 Query 并点击发送（query={query[:50]}{'...' if len(query) > 50 else ''}）...")
                query_placeholder = "Describe the document to generate"
                query_input = page.get_by_placeholder(query_placeholder).or_(
                    page.locator("textarea").first
                )
                query_input.wait_for(state="visible", timeout=timeout_ms)
                query_input.fill(query)
                page.wait_for_timeout(300)
                # 发送按钮为右下角橙色箭头，无 "Generate" 文本；多种选择器兜底
                send_clicked = page.evaluate("""
                    () => {
                        const textarea = document.querySelector('textarea');
                        if (!textarea) return false;
                        let container = textarea.closest('form') || textarea.closest('[class*="input"]') || textarea.parentElement;
                        for (let i = 0; i < 5 && container; i++) {
                            const buttons = container.querySelectorAll('button');
                            if (buttons.length >= 1) {
                                const btn = buttons[buttons.length - 1];
                                if (btn.offsetParent && btn.getBoundingClientRect().width > 0) {
                                    btn.click();
                                    return true;
                                }
                            }
                            container = container.parentElement;
                        }
                        const submit = document.querySelector('button[type="submit"]');
                        if (submit && submit.offsetParent) { submit.click(); return true; }
                        return false;
                    }
                """)
                if not send_clicked:
                    # 兜底：点击输入框右下角区域的按钮（通常为发送箭头）
                    try:
                        box = query_input.bounding_box()
                        if box:
                            page.mouse.click(box["x"] + box["width"] - 50, box["y"] + box["height"] - 30)
                            send_clicked = True
                    except Exception:
                        pass
                if not send_clicked:
                    raise RuntimeError("未找到发送按钮（右下角箭头），请检查页面结构")
                _log("已点击发送，等待 Market Analysis Agent 响应...", "✓")
                page.wait_for_timeout(wait_response_sec * 1000)
                response_text = _extract_market_analysis_agent_response(page, timeout_ms=8000)
                rpa_steps.append("Step 6：输入 Query → 发送 → 等待响应 → 提取")

                log_dir = _root / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                response_file = log_dir / "poffices_response.txt"
                with open(response_file, "w", encoding="utf-8") as f:
                    f.write(f"# Query\n{query}\n\n# Market Analysis Agent Response\n")
                    f.write(response_text if response_text else "(未能自动提取，请查看页面或调试截图)")
                _log(f"响应已保存到: {response_file}", "✓")
                if not response_text:
                    _log("未检测到明显响应区域，可检查页面或 logs/poffices_bootstrap_debug.png", "ℹ")

                # 生成测试报告：输入、输出、调用的 RPA
                try:
                    import importlib.util
                    _report_spec = importlib.util.spec_from_file_location(
                        "generate_report", _root / "scripts" / "generate_report.py"
                    )
                    _report_mod = importlib.util.module_from_spec(_report_spec)
                    _report_spec.loader.exec_module(_report_mod)
                    _report_mod.generate_poffices_report(
                        query=query,
                        response=response_text or "(未提取到响应)",
                        rpas_called=rpa_steps,
                        output_path=log_dir / "poffices_report.html",
                        format="html",
                    )
                    _report_mod.generate_poffices_report(
                        query=query,
                        response=response_text or "(未提取到响应)",
                        rpas_called=rpa_steps,
                        output_path=log_dir / "poffices_report.json",
                        format="json",
                    )
                    _log(f"测试报告已生成: {log_dir / 'poffices_report.html'}, {log_dir / 'poffices_report.json'}", "✓")
                except Exception as e:
                    _log(f"生成测试报告失败: {e}", "⚠")

            return True

        except Exception as e:
            _log(f"执行失败: {e}", "❌")
            try:
                debug_dir = _root / "logs"
                debug_dir.mkdir(parents=True, exist_ok=True)
                screenshot_path = debug_dir / "poffices_bootstrap_debug.png"
                page.screenshot(path=str(screenshot_path))
                _log(f"已保存调试截图: {screenshot_path}", "ℹ")
            except Exception:
                pass
            if not headless:
                _log("浏览器将保持打开，便于调试。按 Enter 关闭...")
                input()
            return False
        finally:
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Poffices.ai RPA Bootstrap")
    parser.add_argument("--headless", action="store_true", help="无头模式")
    parser.add_argument("--full", action="store_true", help="执行完整流程（含 Query 与 Generate，并提取响应）")
    parser.add_argument("--query-test", action="store_true", help="Bootstrap + Query 测试（等同于 --full）")
    parser.add_argument("--query", type=str, default="test", help="Query 测试时的查询内容（默认 test）")
    parser.add_argument("--wait-response", type=int, default=120, help="Query 测试时等待响应的秒数（默认 120）")
    parser.add_argument("--timeout", type=int, default=20, help="每步超时秒数（默认 20）")
    parser.add_argument("--no-slow-mo", action="store_true", help="关闭 slowMo 延时（默认 1 秒）")
    parser.add_argument("--stop-after-login", action="store_true", help="仅执行到登录完成，用于调试选选项前的页面")
    args = parser.parse_args()

    run_full = args.full or args.query_test

    ok = run_bootstrap(
        headless=args.headless,
        run_full=run_full,
        query=args.query,
        wait_response_sec=args.wait_response,
        timeout_ms=args.timeout * 1000,
        slow_mo_ms=0 if args.no_slow_mo else SLOW_MO_MS,
        stop_after_login=getattr(args, "stop_after_login", False),
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
