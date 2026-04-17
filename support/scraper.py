import asyncio
import logging
import os
import json
from playwright.async_api import Page
# 确保从配置中导入必要的变量
from .config import SELECTORS, MAX_WAIT_SECONDS, CHECK_INTERVAL

logger = logging.getLogger(__name__)

class PofficesAutomator:
    def __init__(self, page: Page):
        self.page = page
        self.debug_dir = "/app/api/debug_steps"
        os.makedirs(self.debug_dir, exist_ok=True)

    async def _take_step_screenshot(self, step_name: str):
        """辅助函数：保存调试截图"""
        path = os.path.join(self.debug_dir, f"{step_name}.png")
        await self.page.screenshot(path=path, full_page=True)

    async def handle_welcome_popup(self):
        """处理可能出现的欢迎/引导弹窗"""
        try:
            skip_btn = self.page.get_by_role("button", name="Skip")
            if await skip_btn.is_visible(timeout=5000):
                logger.info("👋 发现欢迎弹窗，正在点击 Skip...")
                await skip_btn.click()
                await asyncio.sleep(1)
        except Exception:
            logger.info("ℹ️ 未发现欢迎弹窗或已自动关闭")

    async def clean_ui_interference(self):
        """移除可能遮挡点击的 UI 层"""
        try:
            await self.page.evaluate("""() => {
                const selectors = ['#cboxoverlay', '.am-tour-overlay', '#am-tour-overlay', '#gen-tour-welcome', '.modal-backdrop'];
                selectors.forEach(s => {
                    document.querySelectorAll(s).forEach(el => el.remove());
                });
                document.body.style.overflow = 'auto';
            }""")
        except: pass

    async def run_detailed_rpa(self, query: str):
        """[功能一] 执行九步法 RPA 模拟操作以触发生成"""
        # 检查是否已经在结果页
        if await self.page.locator("text='Time of completion'").count() > 0:
            logger.info("页面已经处于结果展示状态，跳过前置点击流程")
            return
        
        logger.info("🎬 开始执行 RPA 自动化流程...")
        # 🎯 位置 1: 刚进页面截一张，确认是否加载成功
        await self._take_step_screenshot("step0_init_page")
        await self.page.wait_for_load_state("networkidle") 
        await self.handle_welcome_popup()
        await self.clean_ui_interference()
        await asyncio.sleep(10)

        # 依次执行点击步骤 (根据 SELECTORS 配置)
        try:
            # STEP 1: 点击徽章
            logger.info("🎬 [STEP 1/9] 点击 General Agent Mode ...")
            # 🎯 位置 2: 在执行 wait_for 之前截图
            # 这样你可以看到 15秒等待开始时，页面上到底有没有那个徽章
            await self._take_step_screenshot("step1_before_wait")
            badge = self.page.locator(SELECTORS["mode_badge"])
            try:
                await badge.wait_for(state="visible", timeout=15000)
                await badge.click(force=True)
                logger.info("✅ STEP 1 点击成功")
            except Exception as e:
                # 🎯 位置 3: 专门捕获 STEP 1 超时的瞬间
                await self._take_step_screenshot("step1_TIMEOUT_ERROR")
                raise e

            # STEP 2: 切换模式
            logger.info("🎬 [STEP 2/9] 切换至 Agent Master Mode...")
            await asyncio.sleep(1)
            await self.clean_ui_interference()
            await self.page.click(SELECTORS["master_mode_card"], force=True)

            # STEP 3: 标签切换
            logger.info("🎬 [STEP 3/9] 点击 Agent Master 标签页...")
            await self.page.click(SELECTORS["agent_master_tab"], force=True)
            await asyncio.sleep(1)
            await self._take_step_screenshot("step3_tab_switched")

            # STEP 4: 展开列表
            logger.info("🎬 [STEP 4/9] 展开 General Office 列表...")
            header = self.page.locator(SELECTORS["office_header"])
            await header.scroll_into_view_if_needed()
            await header.click(force=True)
            await self._take_step_screenshot("step4_office_expanded")

            # STEP 5: 添加 Agent、
            logger.info("🎬 [STEP 5/9] 点击 [+] 添加 General Agent...")
            await self.page.click(SELECTORS["add_agent_btn"], force=True)
            await self._take_step_screenshot("step5_agent_added")

            # STEP 6: 应用设置
            logger.info("🎬 [STEP 6/9] 点击 Apply Settings 应用配置...")
            await self.page.click(SELECTORS["apply_settings_btn"], force=True)
            await asyncio.sleep(2)
            await self._take_step_screenshot("step6_applied")

            # STEP 7-8: 填入指令并提交
            logger.info("🎬 [STEP 7/9] 在 Textarea 填入指令...")
            await self.page.fill(SELECTORS["query_textarea"], query)
            await self._take_step_screenshot("step7_query_filled")
            logger.info("🎬 [STEP 8/9] 点击提交按钮 (Enter)...")
            await self.page.click(SELECTORS["submit_btn"], force=True)
            await asyncio.sleep(5)
            await self._take_step_screenshot("step8_submitted")
            
            logger.info(f"🚀 RPA 指令下发成功: {query}")
            await self._take_step_screenshot("step8_submitted")
        except Exception as e:
            logger.error(f"❌ RPA 步骤执行中断: {str(e)}")
            await self._take_step_screenshot("error_rpa_failed")

    async def wait_and_capture_assets(self):

        """[功能一专用] 阻塞式等待 AI 完成并抓取全量资产"""

        logger.info("🕵️ 开始监控 AI 生成进度...")
        elapsed = 0
        last_text_len = 0

        while elapsed < MAX_WAIT_SECONDS:
            await asyncio.sleep(CHECK_INTERVAL)
            elapsed += CHECK_INTERVAL

            # 1. 检查进度条是否还在
            is_loading = await self.page.locator(SELECTORS["loading_spinner"]).count() > 0
            container = self.page.locator(SELECTORS["result_content"])
            current_text = await container.text_content() or ""

            # 只有看到 'Time of completion:' 才算完成
            if not is_loading and len(current_text) > 0 and len(current_text) == last_text_len:

                logger.info(f"🏆 AI 生成完毕，开始深度抓取资产...")
                container = self.page.locator(SELECTORS["result_content"])
                await container.scroll_into_view_if_needed()

                # 滚动以加载图片和链接资产
                for i in range(3):
                    await self.page.mouse.wheel(0, 2000)
                    await asyncio.sleep(2)
                    await self._take_step_screenshot(f"audit_scrolling_{i}")

                

                text = await container.text_content()
                images = await self.page.eval_on_selector_all(SELECTORS["all_images"], "els => els.map(el => el.src)")
                link_els = await self.page.query_selector_all(SELECTORS["all_links"])
                links = []

                for el in link_els:
                    links.append({'href': await el.get_attribute('href'), 'text': (await el.text_content()).strip()})
                return {"text": text, "images": images, "links": links}

            

            last_text_len = len(current_text)
            if elapsed % 60 == 0:
                await self._take_step_screenshot(f"audit_waiting_{elapsed}s")
                logger.warning(f"⏳ AI 仍在生成中... ({elapsed}s)")
        return None
