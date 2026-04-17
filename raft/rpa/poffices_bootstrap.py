"""
Poffices.ai 可复用 Bootstrap 逻辑：可在给定 page 上执行登录、选 Agent、Query 等。
供 scripts/run_poffices_bootstrap.py 与 raft.rpa.poffices_rpa 共用。
"""
import os
import re
import time
from typing import Any, Callable

POFFICES_URL = "https://b1s2.hkrnd.com/"
REAL_BROWSER_FINGERPRINT = {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "viewport": {"width": 1920, "height": 1080},
    "locale": "zh-TW",
    "timezone_id": "Asia/Hong_Kong",
}


def _noop_log(_msg: str, _icon: str = "ℹ") -> None:
    pass


def _dismiss_welcome_popup(page, log_fn: Callable[[str, str], None] = _noop_log) -> bool:
    try:
        got_it = page.get_by_role("button", name="Got it!").or_(page.get_by_text("Got it!"))
        if got_it.first.is_visible(timeout=2000):
            got_it.first.click()
            log_fn("已关闭 Welcome 弹窗", "✓")
            page.wait_for_timeout(500)
            return True
    except Exception:
        pass
    try:
        skip_btn = page.get_by_text("Skip").first
        if skip_btn.is_visible(timeout=1000):
            skip_btn.click()
            log_fn("已关闭引导弹窗", "✓")
            page.wait_for_timeout(500)
            return True
    except Exception:
        pass
    return False


def _enable_page_scroll(page) -> None:
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


def _is_agent_already_selected(page, agent_name: str) -> bool:
    """检测指定 Agent（如 Market Analysis）是否已被选中。"""
    if not (agent_name and agent_name.strip()):
        return False
    try:
        return page.evaluate(
            """
            (agentName) => {
                const els = Array.from(document.querySelectorAll('*')).filter(el =>
                    el.textContent?.trim() === agentName && el.offsetParent !== null
                );
                for (const el of els) {
                    let p = el;
                    for (let i = 0; i < 8 && p; i++) {
                        if (p.getAttribute?.('aria-selected') === 'true' ||
                            p.getAttribute?.('data-selected') === 'true' ||
                            /selected|active|checked|chosen/i.test(p.className || '')) return true;
                        p = p.parentElement;
                    }
                }
                return false;
            }
            """,
            agent_name.strip(),
        )
    except Exception:
        return False


def _is_market_analysis_already_selected(page) -> bool:
    """兼容旧调用：检测 Market Analysis 是否已选中。"""
    return _is_agent_already_selected(page, "Market Analysis")


def _is_business_office_expanded(page, agent_name: str = "Market Analysis") -> bool:
    """检测 Business Office 列表是否已展开（能看到其子项，如指定 agent_name）。"""
    if not (agent_name and agent_name.strip()):
        agent_name = "Market Analysis"
    try:
        return page.evaluate(
            """
            (agentName) => {
                const bo = Array.from(document.querySelectorAll('*')).find(el =>
                    el.textContent?.trim() === 'Business Office' && el.offsetParent !== null
                );
                if (!bo) return false;
                let container = bo.parentElement;
                for (let i = 0; i < 6 && container; i++) {
                    const kids = Array.from(container.querySelectorAll('*'));
                    const has = kids.some(el =>
                        el.offsetParent !== null &&
                        el.textContent?.trim() === agentName
                    );
                    if (has) return true;
                    container = container.parentElement;
                }
                return false;
            }
            """,
            agent_name.strip(),
        )
    except Exception:
        return False


def _clear_selected_agents_in_agent_master_if_any(
    page,
    timeout_ms: int = 5000,
    log_fn: Callable[[str, str], None] = _noop_log,
) -> None:
    """若 Agent Master Settings 右侧已有已选 Agent，先点 Clear All 清空，防止历史残留。"""
    try:
        clear_btn = page.get_by_role("button", name="Clear All").or_(page.get_by_text("Clear All", exact=True)).first
        if clear_btn.is_visible(timeout=min(2000, timeout_ms)):
            clear_btn.click()
            page.wait_for_timeout(500)
            log_fn("已清空右侧已选 Agent（防止历史残留）", "✓")
    except Exception:
        pass


def _is_apply_needed(page) -> bool:
    """检测 Apply 按钮是否可见且可点击（未 apply 或配置已变更时才需要点击）。"""
    try:
        apply_btn = page.locator(
            'button:has-text("Apply"), [role="button"]:has-text("Apply")'
        ).first
        if not apply_btn.is_visible(timeout=2000):
            return False
        # 若按钮存在但被禁用，说明已是最新状态，无需点击
        is_disabled = apply_btn.is_disabled()
        return not is_disabled
    except Exception:
        return True  # 检测失败时保守地认为需要点击


def _is_agent_master_mode_already_on(page) -> bool:
    """检测 Enable Agent Master Mode 开关是否已开启。

    Bug fix：querySelectorAll 是前序遍历，find 会命中包含文字的父容器（bounding box
    覆盖整行），导致位置过滤把开关排除在外，始终返回 False。
    修复：取所有匹配里最后一个（最深的叶节点），再向上找最近的 switch 邻居，
    不依赖视觉位置判断。
    """
    # 策略1：Playwright 原生 label 关联（最可靠）
    try:
        toggle = page.get_by_label("Enable Agent Master Mode")
        if toggle.count() > 0 and toggle.first.is_visible(timeout=1000):
            return bool(toggle.first.is_checked())
    except Exception:
        pass

    # 策略2：DOM 深度搜索——取最深匹配节点，向上找最近 switch
    try:
        return page.evaluate("""
            () => {
                const LABEL = 'Enable Agent Master Mode';

                // 取所有文本恰好匹配的元素，用最后一个（最深/最小的叶节点）
                const matches = Array.from(document.querySelectorAll('*')).filter(el =>
                    el.offsetParent !== null && el.textContent?.trim() === LABEL
                );
                const label = matches[matches.length - 1];
                if (!label) return false;

                // 判断一个元素是否处于"开启"状态（覆盖常见 UI 框架）
                const isOn = (el) => {
                    if (!el || el.offsetParent === null) return null; // null = 未知
                    // 原生 checkbox
                    if (el.tagName === 'INPUT' && el.type === 'checkbox') return el.checked;
                    // ARIA
                    const ac = el.getAttribute('aria-checked');
                    if (ac === 'true')  return true;
                    if (ac === 'false') return false;
                    // Radix UI / shadcn
                    const ds = el.getAttribute('data-state');
                    if (ds === 'checked' || ds === 'on')         return true;
                    if (ds === 'unchecked' || ds === 'off')      return false;
                    // data-checked
                    const dc = el.getAttribute('data-checked');
                    if (dc === 'true' || dc === '')  return true;
                    if (dc === 'false')               return false;
                    // Tailwind 颜色类（开启时通常为蓝/靛/绿）
                    const cls = el.className || '';
                    if (/\\bbg-(blue|indigo|primary|sky|teal|green)-(4|5|6|7|8|9)00\\b/.test(cls)) return true;
                    return null; // 未能判断
                };

                // 从 label 向上爬，找同级/子级的 switch 元素
                let node = label;
                for (let depth = 0; depth < 8 && node; depth++) {
                    const parent = node.parentElement;
                    if (!parent) break;
                    const candidates = Array.from(parent.querySelectorAll(
                        '[role="switch"], [role="checkbox"], input[type="checkbox"], button[aria-checked]'
                    )).filter(el => el.offsetParent !== null);

                    for (const sw of candidates) {
                        const state = isOn(sw);
                        if (state === true)  return true;
                        if (state === false) return false;
                    }
                    node = parent;
                }
                return false;
            }
        """)
    except Exception:
        return False


def _ensure_agent_master_mode_on(
    page,
    timeout_ms: int = 10_000,
    log_fn: Callable[[str, str], None] = _noop_log,
) -> None:
    """
    确保 Enable Agent Master Mode 开关处于开启状态（幂等 + 自我纠正）。

    - 已开启 → 直接返回，不点击
    - 未开启 → 点击，验证；若点击后反而关闭（误判/误点），自动再点一次纠正
    - 最多点击 2 次，超出后记录警告
    """
    def _click_toggle():
        enable_text = page.get_by_text("Enable Agent Master Mode").first
        enable_text.wait_for(state="visible", timeout=timeout_ms)
        enable_text.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        clicked = page.evaluate("""
            () => {
                const label = Array.from(document.querySelectorAll('*')).filter(el =>
                    el.offsetParent !== null && el.textContent?.trim() === 'Enable Agent Master Mode'
                ).pop();
                if (!label) return false;
                let node = label;
                for (let i = 0; i < 8 && node; i++) {
                    const parent = node.parentElement;
                    if (!parent) break;
                    const sw = Array.from(parent.querySelectorAll(
                        '[role="switch"], [role="checkbox"], input[type="checkbox"], button[aria-checked]'
                    )).find(el => el.offsetParent !== null);
                    if (sw) { sw.click(); return true; }
                    node = parent;
                }
                // 兜底：点击 label 右侧 100px
                const rect = label.getBoundingClientRect();
                const el = document.elementFromPoint(rect.right + 100, rect.top + rect.height / 2);
                if (el && el !== document.body) { el.click(); return true; }
                return false;
            }
        """)
        if not clicked:
            box = page.get_by_text("Enable Agent Master Mode").first.bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] + 100, box["y"] + box["height"] / 2)
        page.wait_for_timeout(800)

    if _is_agent_master_mode_already_on(page):
        log_fn("Enable Agent Master Mode 已开启，跳过", "✓")
        return

    for attempt in range(2):
        log_fn(f"点击 Enable Agent Master Mode 开关（第 {attempt + 1} 次）", "ℹ")
        _click_toggle()
        if _is_agent_master_mode_already_on(page):
            log_fn("已确认 Enable Agent Master Mode 开启", "✓")
            return
        log_fn("点击后未检测到开启状态，可能误点关闭，尝试再次点击纠正", "⚠")

    log_fn("Enable Agent Master Mode 开关状态无法确认，建议手动检查", "⚠")


def extract_response(page, timeout_ms: int = 5000) -> str:
    """
    从页面提取 Agent 的响应内容。

    策略：优先提取整个主内容区的全文（main / 聊天区域），由调用方用 strip 逻辑
    保留正文、去掉 assignment 等无关内容。若宽泛提取失败，则回退到按块打分选取。
    """
    # 1. 优先：提取主内容区全文，确保 assignment + 报告在同一段时能一起拿到
    broad_selectors = ["main", '[role="main"]', '[class*="chat"]', '[class*="conversation"]']
    for sel in broad_selectors:
        try:
            loc = page.locator(sel).first
            text = loc.inner_text(timeout=min(3000, timeout_ms))
            if text and len(text.strip()) >= 300:
                return text.strip()
        except Exception:
            continue

    # 2. 回退：按块收集，用打分选取最像报告的块
    selectors = [
        '[class*="message"] [class*="content"]', '[class*="response"]', '[class*="output"]',
        '[class*="result"]', 'article', '[class*="markdown"]', '.prose', '[role="article"]',
        'main [class*="content"]',
    ]
    candidates: list[str] = []
    seen: set[str] = set()
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                for i in range(loc.count()):
                    text = loc.nth(i).inner_text(timeout=min(2000, timeout_ms))
                    if not text or len(text.strip()) <= 100:
                        continue
                    t = text.strip()
                    if t in seen:
                        continue
                    seen.add(t)
                    candidates.append(t)
        except Exception:
            continue

    if not candidates:
        return ""

    def _report_score(t: str) -> tuple[int, int]:
        score = 0
        if "参考文献" in t:
            score += 15
        if "简介" in t or "引言" in t:
            score += 8
        if t.count("##") >= 2:
            score += 5
        _assignment_phrases = (
            "I have commenced work", "Work is underway", "strategic implementation",
            "I'm here to report", "document generation process has been initiated",
            "I have been assigned to handle this task", "report on the document generation task",
        )
        _is_assignment = (
            ("Preparing your document" in t or "It approximately takes 2 minutes" in t)
            and any(p.lower() in t.lower() for p in _assignment_phrases)
            and len(t) < 2500
        )
        if _is_assignment:
            score -= 25
        return (score, len(t))

    return max(candidates, key=_report_score)


def run_bootstrap_on_page(
    page,
    *,
    username: str | None = None,
    password: str | None = None,
    agent_name: str = "Market Analysis",
    timeout_ms: int = 15_000,
    log_fn: Callable[[str, str], None] = _noop_log,
    resume_on_current_page: bool = False,
) -> None:
    """
    在给定的 Playwright page 上执行 Poffices Bootstrap（Step 0–5）。
    不包含 Query 测试；调用方需单独调用 fill_query_and_send。
    agent_name：要选择的 Agent 名称（如 Market Analysis、Marketing Plan），默认 Market Analysis。
    resume_on_current_page：为 True 时不执行 page.goto（用于 Discovery 后同一会话内仅选 Agent + Apply）。
    """
    un = username or os.environ.get("POFFICES_USERNAME", "")
    pw = password or os.environ.get("POFFICES_PASSWORD", "")
    if not un or not pw:
        raise ValueError("需要 POFFICES_USERNAME 和 POFFICES_PASSWORD（环境变量或参数）")

    if not resume_on_current_page:
        page.goto(POFFICES_URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        page.wait_for_timeout(1500)
    else:
        page.wait_for_timeout(400)
    _enable_page_scroll(page)

    # 等待首屏关键内容出现（SPA 可能稍晚渲染），避免在空白页上找 Login
    try:
        page.wait_for_selector("body", state="attached", timeout=5000)
        page.wait_for_timeout(2000)
    except Exception:
        pass
    login_or_content = page.locator('button:has-text("Login"), a:has-text("Login"), [role="button"]:has-text("Login"), [role="link"]:has-text("Login"), textarea, [placeholder*="document"], [placeholder*="Username"]').first
    try:
        login_or_content.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        pass

    login_btn = page.locator('button:has-text("Login"), a:has-text("Login"), [role="button"]:has-text("Login"), [role="link"]:has-text("Login")').first
    try:
        login_btn.wait_for(state="visible", timeout=5000)
        need_login = True
    except Exception:
        need_login = False

    if need_login:
        log_fn("执行登录...", "ℹ")
        user_input = page.get_by_placeholder("Username or Email").or_(page.locator('input[name="username"], input[type="text"]').first)
        if not user_input.is_visible():
            login_btn.click()
            page.wait_for_timeout(1500)
            user_input.wait_for(state="visible", timeout=timeout_ms)
        user_input.fill(un)
        page.get_by_placeholder("Password").or_(page.locator('input[name="password"], input[type="password"]').first).fill(pw)
        submit_btn = page.get_by_role("button", name="Login").or_(page.get_by_role("button", name="Log in")).or_(page.locator('button[type="submit"]')).first
        submit_btn.click()
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        page.wait_for_timeout(2000)
        still_on_login = "login" in page.url.lower()
        try:
            still_on_login = still_on_login or page.get_by_role("button", name="Login").is_visible()
        except Exception:
            pass
        if still_on_login:
            page.goto(POFFICES_URL, wait_until="networkidle", timeout=timeout_ms)
            page.wait_for_timeout(2000)
        _enable_page_scroll(page)
        log_fn("登录完成", "✓")

    page.wait_for_timeout(2000)
    _dismiss_welcome_popup(page, log_fn)

    doc_input = page.get_by_placeholder("Describe the document to generate").or_(page.locator("textarea").first)
    doc_input.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(1000)
    try:
        poffices_visible = page.get_by_text("Poffices Agent Platform").first.is_visible(timeout=2000)
    except Exception:
        poffices_visible = False
    if not poffices_visible:
        agent_master_opt = page.get_by_text("Agent Master").first
        agent_master_opt.scroll_into_view_if_needed()
        agent_master_opt.wait_for(state="visible", timeout=timeout_ms)
        agent_master_opt.click()
        page.wait_for_timeout(2000)
    _dismiss_welcome_popup(page, log_fn)
    _enable_page_scroll(page)

    # 若右侧已有已选 Agent，先 Clear All 防止历史残留
    _clear_selected_agents_in_agent_master_if_any(page, timeout_ms=timeout_ms, log_fn=log_fn)

    # 用搜索框定位 Agent（不依赖具体 Office 位置，适用于 Research Proposal 等任意 Agent）
    agent_display = agent_name.strip() or "Market Analysis"
    if _is_agent_already_selected(page, agent_display):
        log_fn(f"{agent_display} 已选中，跳过", "✓")
    else:
        search_box = page.get_by_placeholder("Search agents or offices...").first
        search_box.wait_for(state="visible", timeout=timeout_ms)
        search_box.click()
        page.wait_for_timeout(300)
        search_box.fill("")
        page.wait_for_timeout(200)
        search_box.fill(agent_display)
        page.wait_for_timeout(1500)  # 等待搜索结果过滤
        # 点击搜索结果中的 Agent（排除搜索框本身，取列表中的可点击项）
        agent_matches = page.get_by_text(agent_display, exact=True)
        clicked = False
        for i in range(agent_matches.count()):
            el = agent_matches.nth(i)
            try:
                tag = (el.evaluate("e => e.tagName?.toLowerCase()") or "").strip()
                if tag in ("input", "textarea"):
                    continue
                el.scroll_into_view_if_needed()
                el.click()
                clicked = True
                break
            except Exception:
                continue
        if not clicked and agent_matches.count() > 0:
            agent_matches.first.scroll_into_view_if_needed()
            agent_matches.first.click()
        page.wait_for_timeout(1000)

    _ensure_agent_master_mode_on(page, timeout_ms=timeout_ms, log_fn=log_fn)
    page.wait_for_timeout(1500)

    # Apply：仅在按钮可见且未禁用时点击，避免重复 apply 触发不必要的页面刷新
    if _is_apply_needed(page):
        apply_btn = page.locator('button:has-text("Apply"), [role="button"]:has-text("Apply")').first
        apply_btn.wait_for(state="visible", timeout=timeout_ms)
        apply_btn.scroll_into_view_if_needed()
        apply_btn.click()
        page.wait_for_timeout(2000)
        log_fn("已点击 Apply，Bootstrap 完成", "✓")
    else:
        log_fn("Apply 按钮不可用或已是最新状态，跳过", "✓")
    log_fn("Bootstrap 完成", "✓")


def wait_for_generation_complete(
    page,
    *,
    timeout_sec: int = 300,
    log_fn: Callable[[str, str], None] = _noop_log,
) -> None:
    """等待生成完毕：仅以绿色 Toast「Document generation is completed.」为结束标志，看到后稍等 0.5s 即返回。"""
    deadline = time.monotonic() + timeout_sec
    poll_ms = 1500
    post_phrase_wait_sec = 0.5

    while time.monotonic() < deadline:
        try:
            page.get_by_text("Document generation is completed.", exact=False).wait_for(
                state="visible", timeout=poll_ms
            )
            log_fn("已检测到绿色完成标识「Document generation is completed.」", "✓")
            page.wait_for_timeout(int(post_phrase_wait_sec * 1000))
            return
        except Exception:
            pass
        page.wait_for_timeout(1000)

    raise TimeoutError(
        f"等待生成完成超时（{timeout_sec}s）：未检测到绿色标识「Document generation is completed.」。"
    )


def _result_container_locator(page):
    """与参考实现一致：优先主内容区，便于整段抓取报告。"""
    for sel in ("main", '[role="main"]', '[class*="chat"]', '[class*="conversation"]'):
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                return loc.first
        except Exception:
            continue
    return page.locator("body")


def _is_generating_ui(page) -> bool:
    """参考 scraper：是否存在可见的加载/生成中指示（生成结束后应为 False）。"""
    try:
        if page.get_by_text("Document generation is completed.", exact=False).count() > 0:
            try:
                if page.get_by_text("Document generation is completed.", exact=False).first.is_visible(timeout=200):
                    return False
            except Exception:
                pass
    except Exception:
        pass
    try:
        busy = page.locator('[aria-busy="true"]')
        if busy.count() > 0 and busy.first.is_visible(timeout=150):
            return True
    except Exception:
        pass
    try:
        spin = page.locator('[class*="animate-spin"], [class*="loading"] svg, [class*="spinner"]')
        n = min(spin.count(), 8)
        for i in range(n):
            try:
                if spin.nth(i).is_visible(timeout=100):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _snapshot_result_text(page) -> str:
    try:
        loc = _result_container_locator(page)
        # inner_text 与 text_content 不同：不包含 <script>/<style> 内文字，避免整页 dump 成 Elementor/WordPress 脚本噪音
        return (loc.inner_text(timeout=8000) or "").strip()
    except Exception:
        return ""


def capture_assets_from_result_page(
    page,
    *,
    log_fn: Callable[[str, str], None] = _noop_log,
    max_images: int = 200,
    max_links: int = 500,
) -> dict[str, Any]:
    """
    参考 参考文件/scraper.py：滚动结果区后抓取 text / 图片 src / 链接。
    在已确认生成完成后调用；可与 wait_and_capture_assets 搭配。
    """
    container = _result_container_locator(page)
    try:
        container.scroll_into_view_if_needed()
    except Exception:
        pass
    for i in range(3):
        try:
            page.mouse.wheel(0, 2000)
        except Exception:
            page.evaluate("window.scrollBy(0, 2000)")
        page.wait_for_timeout(2000)
        log_fn(f"抓取前滚动 {i + 1}/3", "ℹ")

    text = ""
    try:
        text = (container.inner_text(timeout=8000) or "").strip()
    except Exception:
        pass
    if len(text) < 80:
        fallback = extract_response(page, timeout_ms=8000)
        if fallback and len(fallback.strip()) > len(text):
            text = fallback.strip()

    images: list[str] = []
    try:
        raw_imgs = page.evaluate(
            """() => {
                const els = Array.from(document.querySelectorAll('main img, [role="main"] img, article img'));
                const out = [];
                const seen = new Set();
                for (const el of els) {
                    const s = (el && el.src) ? el.src : '';
                    if (s && !seen.has(s)) { seen.add(s); out.push(s); }
                }
                return out;
            }"""
        )
        if isinstance(raw_imgs, list):
            seen: set[str] = set()
            for u in raw_imgs:
                if isinstance(u, str) and u and u not in seen:
                    seen.add(u)
                    images.append(u)
                    if len(images) >= max_images:
                        break
    except Exception:
        pass

    links: list[dict[str, str | None]] = []
    try:
        for el in page.query_selector_all("main a[href], [role='main'] a[href]"):
            try:
                href = el.get_attribute("href")
                lt = el.text_content()
                t = (lt or "").strip() if lt else ""
                if href:
                    links.append({"href": href, "text": t})
            except Exception:
                continue
            if len(links) >= max_links:
                break
    except Exception:
        pass

    return {"text": text, "images": images, "links": links}


def wait_and_capture_assets(
    page,
    *,
    timeout_sec: int = 300,
    check_interval_sec: float = 2.0,
    log_fn: Callable[[str, str], None] = _noop_log,
) -> dict[str, Any]:
    """
    参考 参考文件/scraper.wait_and_capture_assets：轮询直至加载结束且结果区文本长度稳定，再抓取全量资产。
    与绿色 Toast 兼容：一旦可见「Document generation is completed.」则直接抓取（与旧逻辑一致）。
    """
    deadline = time.monotonic() + timeout_sec
    last_len = -1
    interval_ms = max(500, int(check_interval_sec * 1000))

    while time.monotonic() < deadline:
        page.wait_for_timeout(interval_ms)

        try:
            done = page.get_by_text("Document generation is completed.", exact=False)
            if done.count() > 0 and done.first.is_visible(timeout=400):
                log_fn("检测到生成完成 Toast，抓取资产", "✓")
                return capture_assets_from_result_page(page, log_fn=log_fn)
        except Exception:
            pass

        loading = _is_generating_ui(page)
        current = _snapshot_result_text(page)
        cur_len = len(current)

        # "Time of completion" 仅在时间戳已填入（非占位符 "s"）时才算完成
        import re as _re
        _completion_filled = bool(
            _re.search(r"Time of completion:\s*\S{4,}", current)
        ) and "Time of completion: s" not in current

        if (
            not loading
            and cur_len > 0
            and cur_len == last_len
            and cur_len >= 120
            and (
                _completion_filled
                or "Document generation is completed" in current
                or "参考文献" in current
                or cur_len >= 2000
            )
        ):
            log_fn("结果区文本已稳定，抓取资产", "✓")
            return capture_assets_from_result_page(page, log_fn=log_fn)

        last_len = cur_len

    log_fn("轮询未在时限内稳定，回退为 Toast 等待后抓取", "⚠")
    try:
        wait_for_generation_complete(
            page,
            timeout_sec=min(120, max(60, timeout_sec // 3)),
            log_fn=log_fn,
        )
    except TimeoutError as e:
        raise TimeoutError(
            f"wait_and_capture_assets 超时（{timeout_sec}s）：未检测到完成且结果未稳定。"
        ) from e
    return capture_assets_from_result_page(page, log_fn=log_fn)


def click_new_question(
    page,
    *,
    timeout_ms: int = 10_000,
    log_fn: Callable[[str, str], None] = _noop_log,
) -> None:
    """在已生成响应的页面上点击 New question，准备输入下一个 query。"""
    new_question_btn = (
        page.get_by_role("button", name="New question")
        .or_(page.get_by_text("New question", exact=False))
    ).first
    new_question_btn.wait_for(state="visible", timeout=timeout_ms)
    new_question_btn.click()
    page.wait_for_timeout(1000)
    log_fn("已点击 New question，可输入下一轮 query", "✓")


def select_agent_on_current_page(
    page,
    agent_name: str,
    *,
    timeout_ms: int = 20_000,
    log_fn: Callable[[str, str], None] = _noop_log,
) -> None:
    """
    在当前已打开的 Poffices 页面上仅执行「选择指定 Agent」：确保侧栏/Agent Master 可见，
    搜索并选择 agent_name，Apply。不包含 goto/登录；用于多 Agent 测试时在同一会话内切换 Agent。
    """
    agent_display = (agent_name or "").strip() or "Market Analysis"
    page.wait_for_timeout(1500)  # 等待页面稳定（New question 后侧栏可能尚未就绪）
    try:
        poffices_visible = page.get_by_text("Poffices Agent Platform").first.is_visible(timeout=3000)
    except Exception:
        poffices_visible = False
    if not poffices_visible:
        agent_master_opt = page.get_by_text("Agent Master").first
        agent_master_opt.scroll_into_view_if_needed()
        agent_master_opt.wait_for(state="visible", timeout=timeout_ms)
        agent_master_opt.click()
        page.wait_for_timeout(2500)
    _dismiss_welcome_popup(page, log_fn)
    _enable_page_scroll(page)
    _clear_selected_agents_in_agent_master_if_any(page, timeout_ms=timeout_ms, log_fn=log_fn)
    if _is_agent_already_selected(page, agent_display):
        log_fn(f"{agent_display} 已选中，跳过", "✓")
    else:
        search_box = page.get_by_placeholder("Search agents or offices...").first
        search_box.wait_for(state="visible", timeout=timeout_ms)
        search_box.click()
        page.wait_for_timeout(300)
        search_box.fill("")
        page.wait_for_timeout(200)
        search_box.fill(agent_display)
        page.wait_for_timeout(1500)
        agent_matches = page.get_by_text(agent_display, exact=True)
        clicked = False
        for i in range(agent_matches.count()):
            el = agent_matches.nth(i)
            try:
                tag = (el.evaluate("e => e.tagName?.toLowerCase()") or "").strip()
                if tag in ("input", "textarea"):
                    continue
                el.scroll_into_view_if_needed()
                el.click()
                clicked = True
                break
            except Exception:
                continue
        if not clicked and agent_matches.count() > 0:
            agent_matches.first.scroll_into_view_if_needed()
            agent_matches.first.click()
        page.wait_for_timeout(1000)
    _ensure_agent_master_mode_on(page, timeout_ms=timeout_ms, log_fn=log_fn)
    page.wait_for_timeout(1500)
    if _is_apply_needed(page):
        apply_btn = page.locator('button:has-text("Apply"), [role="button"]:has-text("Apply")').first
        apply_btn.wait_for(state="visible", timeout=timeout_ms)
        apply_btn.scroll_into_view_if_needed()
        apply_btn.click()
        page.wait_for_timeout(2000)
        log_fn("已点击 Apply，切换 Agent 完成", "✓")
    else:
        log_fn("Apply 按钮不可用或已是最新状态，跳过", "✓")


def ensure_agent_master_panel_visible(
    page,
    *,
    username: str | None = None,
    password: str | None = None,
    timeout_ms: int = 15_000,
    log_fn: Callable[[str, str], None] = _noop_log,
) -> None:
    """
    最小化 Bootstrap：登录 + 打开 Agent Master 面板，不选择任何 Agent。
    供 Discovery 阶段（list_offices、expand_office 等）前置使用。
    """
    un = username or os.environ.get("POFFICES_USERNAME", "")
    pw = password or os.environ.get("POFFICES_PASSWORD", "")
    if not un or not pw:
        raise ValueError("需要 POFFICES_USERNAME 和 POFFICES_PASSWORD")

    page.goto(POFFICES_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=timeout_ms)
    page.wait_for_timeout(1500)
    _enable_page_scroll(page)

    try:
        page.wait_for_selector("body", state="attached", timeout=5000)
        page.wait_for_timeout(2000)
    except Exception:
        pass

    login_btn = page.locator('button:has-text("Login"), a:has-text("Login")').first
    try:
        if login_btn.is_visible(timeout=5000):
            log_fn("执行登录...", "ℹ")
            user_input = page.get_by_placeholder("Username or Email").or_(page.locator('input[name="username"]').first)
            if not user_input.is_visible():
                login_btn.click()
                page.wait_for_timeout(1500)
            user_input.fill(un)
            page.get_by_placeholder("Password").or_(page.locator('input[name="password"]').first).fill(pw)
            page.get_by_role("button", name="Login").or_(page.locator('button[type="submit"]')).first.click()
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            page.wait_for_timeout(2000)
            log_fn("登录完成", "✓")
    except Exception:
        pass

    page.wait_for_timeout(2000)
    _dismiss_welcome_popup(page, log_fn)

    doc_input = page.get_by_placeholder("Describe the document to generate").or_(page.locator("textarea").first)
    doc_input.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(1000)

    try:
        poffices_visible = page.get_by_text("Poffices Agent Platform").first.is_visible(timeout=2000)
    except Exception:
        poffices_visible = False
    if not poffices_visible:
        agent_master_opt = page.get_by_text("Agent Master").first
        agent_master_opt.scroll_into_view_if_needed()
        agent_master_opt.wait_for(state="visible", timeout=timeout_ms)
        agent_master_opt.click()
        page.wait_for_timeout(2000)
    _dismiss_welcome_popup(page, log_fn)
    _enable_page_scroll(page)
    log_fn("Agent Master 面板已就绪", "✓")


def list_offices(
    page,
    *,
    timeout_ms: int = 10_000,
) -> list[str]:
    """
    从 Agent Master 左侧面板抓取所有 office 名称（如 Research Office、Business Office）。
    返回纯 office 名列表，不含 (x/y selected) 后缀。
    """
    try:
        search_box = page.get_by_placeholder("Search agents or offices...").first
        search_box.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        return []

    offices = page.evaluate("""
        () => {
            const results = [];
            const re = /^(.+?)\\s*\\(\\d+\\/\\d+\\s*selected\\)$/;
            const all = Array.from(document.querySelectorAll('*'));
            const seen = new Set();
            for (const el of all) {
                const text = (el.textContent || '').trim();
                if (!text || text.length > 80) continue;
                const m = text.match(re);
                if (m) {
                    const name = m[1].trim();
                    if (name && !seen.has(name) && !/^\\d+$/.test(name)) {
                        seen.add(name);
                        results.push(name);
                    }
                }
            }
            return results;
        }
    """)
    if isinstance(offices, list):
        return [str(o).strip() for o in offices if o and str(o).strip()]
    return []


def expand_office(
    page,
    office_name: str,
    *,
    timeout_ms: int = 10_000,
    log_fn: Callable[[str, str], None] = _noop_log,
) -> bool:
    """
    点击展开指定 office。若已展开则跳过。
    优先匹配侧栏中带 (x/y selected) 的整行文案，避免误点其它区域同名片段导致卡住。
    返回是否成功。
    """
    if not (office_name and office_name.strip()):
        return False
    office_name = office_name.strip()
    try:
        # 与 list_offices 一致：侧栏 office 行形如 "HR Office (1/5 selected)"
        row_pat = re.compile(rf"{re.escape(office_name)}\s*\(\s*\d+\s*/\s*\d+\s*selected", re.I)
        loc = page.get_by_text(row_pat).first
        try:
            loc.wait_for(state="visible", timeout=min(timeout_ms, 12_000))
        except Exception:
            loc = page.get_by_text(office_name, exact=False).first
            loc.wait_for(state="visible", timeout=timeout_ms)
        loc.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        loc.click()
        page.wait_for_timeout(500)
        log_fn(f"已展开 {office_name}", "✓")
        return True
    except Exception as e:
        log_fn(f"展开 {office_name} 失败: {e}", "⚠")
        return False


def list_agents_in_office(
    page,
    office_name: str | None = None,
    *,
    timeout_ms: int = 10_000,
) -> list[str]:
    """
    抓取可见 agent 名称列表。

    - 若传入 ``office_name``（与 list_offices / match_office 返回的 office 名一致），
      **仅**在该 office 行对应的子树 + 其后同级兄弟节点子树内收集（展开后 agent 通常在此范围内），
      不会混入其它 Office 下的 agent。
    - 若未传入 office_name，则在 Agent Master 左侧面板根节点内收集（避免全页扫描导致长时间卡顿）。
    """
    _ = timeout_ms  # 预留：与 Playwright 超时配合；抓取逻辑已缩小 DOM 规模

    if office_name and str(office_name).strip():
        oname = str(office_name).strip()
        agents = page.evaluate(
            """
            (officeName) => {
                const skipPatterns = [
                    /\\d+\\/\\d+\\s*selected/i,
                    /Search agents or offices/i,
                    /Agent Master/i,
                    /Enable Agent Master Mode/i,
                    /Clear All/i,
                    /Apply/i,
                    /Start Building/i,
                    /Select/i,
                    /Drag to reorder/i,
                    /Same agent can be selected/i,
                ];
                const isSkip = (t) => skipPatterns.some(p => p.test(t));
                const officeRe = /^(.+?)\\s*\\(\\d+\\/\\d+\\s*selected\\)$/i;
                const target = officeName.trim().toLowerCase();
                const nameMatchesOffice = (t) => {
                    const m = t.match(officeRe);
                    return m && m[1].trim().toLowerCase() === target;
                };

                let headerEl = null;
                for (const el of document.querySelectorAll('*')) {
                    const t = (el.textContent || '').trim();
                    if (t.length > 120) continue;
                    if (nameMatchesOffice(t)) {
                        headerEl = el;
                        break;
                    }
                }
                if (!headerEl) {
                    for (const el of document.querySelectorAll('*')) {
                        const t = (el.textContent || '').trim();
                        if (t.length > 120) continue;
                        const m = t.match(officeRe);
                        if (!m) continue;
                        const oname = m[1].trim().toLowerCase();
                        if (oname !== target && !oname.includes(target) && !target.includes(oname)) continue;
                        headerEl = el;
                        break;
                    }
                }
                if (!headerEl) return [];

                const scopeNodes = [];
                const pushSubtree = (root) => {
                    if (!root) return;
                    scopeNodes.push(root);
                    for (const el of root.querySelectorAll('*')) {
                        scopeNodes.push(el);
                    }
                };
                pushSubtree(headerEl);
                let sib = headerEl.nextElementSibling;
                while (sib) {
                    const t = (sib.textContent || '').trim();
                    const m = t.match(officeRe);
                    if (m && t.length < 100) {
                        const oname = m[1].trim().toLowerCase();
                        if (oname !== target && !oname.includes(target) && !target.includes(oname)) {
                            break;
                        }
                    }
                    pushSubtree(sib);
                    sib = sib.nextElementSibling;
                }

                const results = [];
                const seen = new Set();
                // Office 名（类别）不是 Agent：侧栏常单独显示「HR Office」无 (x/y selected)，必须与 Office 行一并排除
                const isOfficeNoise = (text) => {
                    const low = text.trim().toLowerCase();
                    if (low === target) return true;
                    const m = text.match(officeRe);
                    if (m && text.length < 100) return true; // 任意 Office 行 (n/m selected)
                    if (m) {
                        const oname = m[1].trim().toLowerCase();
                        if (oname === target || oname.includes(target) || target.includes(oname)) return true;
                    }
                    return false;
                };
                for (const el of scopeNodes) {
                    if (el === headerEl) continue;
                    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') continue;
                    const text = (el.textContent || '').trim();
                    if (!text || text.length < 3 || text.length > 80) continue;
                    if (isSkip(text)) continue;
                    if (isOfficeNoise(text)) continue;
                    if (/^\\(.*\\)$/.test(text)) continue;
                    if (/^\\d+$/.test(text)) continue;
                    const children = el.querySelectorAll('*');
                    const hasChildWithSameText = Array.from(children).some(c =>
                        (c.textContent || '').trim() === text && c !== el
                    );
                    if (hasChildWithSameText) continue;
                    if (!seen.has(text)) {
                        seen.add(text);
                        results.push(text);
                    }
                }
                return results.filter(t => t && !/^[\\s\\d\\(\\)]+$/.test(t)).slice(0, 80);
            }
            """,
            oname,
        )
    else:
        agents = page.evaluate(
            """
            () => {
                const skipPatterns = [
                    /\\d+\\/\\d+\\s*selected/i,
                    /Search agents or offices/i,
                    /Agent Master/i,
                    /Enable Agent Master Mode/i,
                    /Clear All/i,
                    /Apply/i,
                    /Start Building/i,
                    /Select/i,
                    /Drag to reorder/i,
                    /Same agent can be selected/i,
                ];
                const isSkip = (t) => skipPatterns.some(p => p.test(t));
                const officeRe = /^(.+?)\\s*\\(\\d+\\/\\d+\\s*selected\\)$/i;
                const inp = document.querySelector(
                    '[placeholder*="Search agents"], [placeholder*="search agents"]'
                );
                let root = document.body;
                if (inp) {
                    let n = inp;
                    for (let i = 0; i < 14 && n; i++) {
                        n = n.parentElement;
                        if (!n) break;
                        const r = n.getBoundingClientRect();
                        if (r.width > 180 && r.height > 240) {
                            root = n;
                            break;
                        }
                    }
                    if (root === document.body) {
                        root = inp.closest("aside") || inp.parentElement || document.body;
                    }
                }
                const all = Array.from(root.querySelectorAll('*'));
                const results = [];
                const seen = new Set();
                for (const el of all) {
                    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') continue;
                    const text = (el.textContent || '').trim();
                    if (!text || text.length < 3 || text.length > 80) continue;
                    if (isSkip(text)) continue;
                    if (officeRe.test(text) && text.length < 100) continue;
                    if (/^\\(.*\\)$/.test(text)) continue;
                    if (/^\\d+$/.test(text)) continue;
                    const children = el.querySelectorAll('*');
                    const hasChildWithSameText = Array.from(children).some(c =>
                        (c.textContent || '').trim() === text && c !== el
                    );
                    if (hasChildWithSameText) continue;
                    if (!seen.has(text)) {
                        seen.add(text);
                        results.push(text);
                    }
                }
                return results.filter(t => t && !/^[\\s\\d\\(\\)]+$/.test(t)).slice(0, 150);
            }
            """
        )

    if isinstance(agents, list):
        out = [str(a).strip() for a in agents if a and len(str(a).strip()) >= 2]
        return out[:50]
    return []


def fill_query_and_send(
    page,
    query: str,
    *,
    timeout_ms: int = 15_000,
) -> None:
    """在已 Bootstrap 或已点 New question 的页面上：定位底部输入框 → 点击中间空白处 → 输入 query → 点击发送。
    多轮时页面上有多个 textarea（历史消息为 readonly+hidden），必须排除它们，只取可见可编辑的。"""
    # 1. 定位可见可编辑的输入框（排除 readonly、aria-hidden 的历史 textarea）
    query_input = page.locator("textarea:not([readonly]):not([aria-hidden='true'])").last
    query_input.wait_for(state="visible", timeout=timeout_ms)
    page.wait_for_timeout(200)

    # 2. 点击输入框中间空白处以聚焦
    box = query_input.bounding_box()
    if box:
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        page.mouse.click(cx, cy)
    else:
        query_input.click()
    page.wait_for_timeout(150)

    # 3. 输入 query 文本（用 press_sequentially 模拟按键，触发 React/Vue 等框架的输入事件）
    query_input.clear()
    query_input.press_sequentially(query, delay=50)
    page.wait_for_timeout(200)

    # 4. 点击右下角发送：用可见可编辑的 textarea 所在容器找按钮
    send_clicked = page.evaluate("""
        () => {
            const all = document.querySelectorAll('textarea');
            const textareas = Array.from(all).filter(t => !t.readOnly && t.getAttribute('aria-hidden') !== 'true');
            const textarea = textareas.length ? textareas[textareas.length - 1] : null;
            if (!textarea) return false;
            let container = textarea.closest('form') || textarea.closest('[class*="input"]') || textarea.parentElement;
            for (let i = 0; i < 6 && container; i++) {
                const buttons = Array.from(container.querySelectorAll('button'));
                if (buttons.length >= 1) {
                    // 取最后一个（通常为右下角发送按钮）
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
        # 备用：根据 textarea 的 bounding box，点击其右下角偏右位置
        try:
            box = query_input.bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] - 40, box["y"] + box["height"] - 25)
                send_clicked = True
        except Exception:
            pass
    if not send_clicked:
        raise RuntimeError("未找到发送按钮（右下角箭头）")


# ========== Agent Master 协作流程：清空 + 按顺序选 Agent + Next 步进 ==========


def clear_selected_agents(
    page,
    *,
    timeout_ms: int = 5000,
    log_fn: Callable[[str, str], None] = _noop_log,
) -> None:
    """
    清空 Agent Master 右侧 Selected Agents 列表。
    若 Clear All 按钮不可见（列表已空）则跳过。
    """
    return _clear_selected_agents_in_agent_master_if_any(
        page, timeout_ms=timeout_ms, log_fn=log_fn
    )


def add_agent_to_flow(
    page,
    agent_name: str,
    *,
    timeout_ms: int = 15_000,
    log_fn: Callable[[str, str], None] = _noop_log,
) -> bool:
    """
    在 Agent Master 搜索框输入 agent_name，点击搜索结果中的 Agent 将其加入右侧 Selected Agents。
    顺序由调用顺序决定：先 add 的为 Step1，后 add 的为 Step2/3...
    返回是否成功添加。
    """
    if not (agent_name and agent_name.strip()):
        return False
    agent_display = agent_name.strip()
    try:
        search_box = page.get_by_placeholder("Search agents or offices...").first
        search_box.wait_for(state="visible", timeout=timeout_ms)
        search_box.click()
        page.wait_for_timeout(300)
        search_box.fill("")
        page.wait_for_timeout(200)
        search_box.fill(agent_display)
        page.wait_for_timeout(1500)
        agent_matches = page.get_by_text(agent_display, exact=True)
        clicked = False
        for i in range(agent_matches.count()):
            el = agent_matches.nth(i)
            try:
                tag = (el.evaluate("e => e.tagName?.toLowerCase()") or "").strip()
                if tag in ("input", "textarea"):
                    continue
                el.scroll_into_view_if_needed()
                el.click()
                clicked = True
                break
            except Exception:
                continue
        if not clicked and agent_matches.count() > 0:
            agent_matches.first.scroll_into_view_if_needed()
            agent_matches.first.click()
        page.wait_for_timeout(800)
        log_fn(f"已添加 Agent: {agent_display}", "✓")
        return True
    except Exception as e:
        log_fn(f"add_agent_to_flow({agent_display}) 失败: {e}", "⚠")
        return False


def has_next_step(
    page,
    *,
    timeout_ms: int = 2000,
) -> bool:
    """
    检测当前页面是否还有 Next 按钮（Agent Master 多步流程中，点击可进入下一 Step）。
    返回 True 表示还有下一步，False 表示已是最后一步（如 Integration 完成）。
    """
    try:
        next_btn = (
            page.get_by_role("button", name="Next")
            .or_(page.get_by_text("Next", exact=False))
            .first
        )
        return next_btn.is_visible(timeout=timeout_ms)
    except Exception:
        return False


def click_next_step(
    page,
    *,
    timeout_ms: int = 10_000,
    log_fn: Callable[[str, str], None] = _noop_log,
) -> None:
    """点击 Next 按钮，进入 Agent Master 流程的下一步。"""
    next_btn = (
        page.get_by_role("button", name="Next")
        .or_(page.get_by_text("Next", exact=False))
    ).first
    next_btn.wait_for(state="visible", timeout=timeout_ms)
    next_btn.scroll_into_view_if_needed()
    next_btn.click()
    page.wait_for_timeout(1500)
    log_fn("已点击 Next，进入下一步", "✓")
