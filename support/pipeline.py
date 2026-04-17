import time
import asyncio
import logging
import os
import json
import re
from playwright.async_api import async_playwright
from fastapi import Request
from fastapi.responses import StreamingResponse

# 导入配置与工具类
from .config import BASE_URL, AUTH_PATH, SCORING_CONFIG
from .scraper import PofficesAutomator
from .auditors.text_auditor import TextAuditor
from .auditors.image_auditor import ImageAuditor
from .auditors.ref_auditor import RefAuditor

logger = logging.getLogger(__name__)

# ==========================================
# 1. 模型加载 (单例模式)
# ==========================================
from sentence_transformers import SentenceTransformer
from transformers import CLIPProcessor, CLIPModel

logger.info("🧠 正在载入多模态审计大脑...")
_sbert = SentenceTransformer(SCORING_CONFIG['models']['semantic'])
_clip_m = CLIPModel.from_pretrained(SCORING_CONFIG['models']['clip'])
_clip_p = CLIPProcessor.from_pretrained(SCORING_CONFIG['models']['clip'])

CACHE_PATH = "/app/api/rpa_cache.json"

# ==========================================
# 2. 核心辅助函数
# ==========================================

async def get_browser_context(p):
    """统一浏览器管理，确保 120s 超时以应对长耗时生成"""
    browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
    context = await browser.new_context(storage_state=AUTH_PATH) if os.path.exists(AUTH_PATH) else await browser.new_context()
    page = await context.new_page()
    page.set_default_timeout(120000) 
    return browser, page

def extract_input_data(body: dict):
    """解析输入并清洗变量，去除变量中的大括号"""
    query = "Marketing Plan"
    extracted_data = None
    if "messages" in body and len(body["messages"]) > 0:
        content_str = body["messages"][-1].get("content", "")
        try:
            payload = json.loads(content_str)
            if isinstance(payload, dict):
                extracted_data = payload.get("extracted_data")
                query = payload.get("message", query)
        except: pass
    if not extracted_data: extracted_data = body.get("extracted_data")
    query = re.sub(r'[{}]', '', str(query)).strip()
    return query, extracted_data

def format_ui_json_display(data: dict):
    """
    🎯 核心修复：解决 UI 界面 JSON 不换行的问题
    Markdown 渲染器需要两个换行符 (\\n\\n) 才能正确分段。
    """
    # 深度拷贝，防止修改原始数据
    display_data = json.loads(json.dumps(data)) 
    if "text" in display_data:
        # 1. 将物理换行符转换为双换行，确保 Markdown 渲染出段落感
        display_data["text"] = display_data["text"].replace('\n', '\n\n')
    
    # 2. 使用 indent=4 确保 JSON 字符串在 UI 显示时具有层级缩进
    return json.dumps(display_data, indent=4, ensure_ascii=False)

# ==========================================
# 3. 功能一：流式 RPA (显示进度并爆发 JSON 结果)
# ==========================================

async def run_f1_rpa(request: Request):
    try:
        body = await request.json()
    except:
        body = {}
    
    query, _ = extract_input_data(body)
    resp_id = f"chatcmpl-{int(time.time())}"

    async def event_generator():
        def create_chunk(content=None, data=None, finish=False):
            # 将输出包装成标准格式
            payload = {
                'id': resp_id, 
                'choices': [{
                    'index': 0, 
                    'delta': {'content': str(content)} if content else {},
                    'finish_reason': 'stop' if finish else None
                }]
            }
            if data: payload['extracted_data'] = data
            return f"data: {json.dumps(payload)}\n\n"

        # 1. 进度反馈
        yield create_chunk(content="🎬 **Wesley Agent 正在连接服务器...**\n")
        
        # 2. 缓存处理逻辑
        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH, 'r') as f:
                    cache = json.load(f)
                    if cache.get("query") == query:
                        yield create_chunk(content=f"♻️ **命中本地缓存（长度：{len(cache['data'].get('text',''))}字）**...\n\n")
                        
                        # 🎯 核心修复 1：将 JSON 格式化并处理换行
                        display_data = json.loads(json.dumps(cache['data']))
                        if "text" in display_data:
                            # Markdown 渲染器需要 \n\n 才能分段
                            display_data["text"] = display_data["text"].replace('\n', '\n\n')
                        
                        full_json_str = json.dumps(display_data, indent=4, ensure_ascii=False)
                        
                        # 🎯 核心修复 2：分段传输（每 2000 字符一块）防止超过缓冲区限制
                        chunk_size = 2000
                        for i in range(0, len(full_json_str), chunk_size):
                            chunk_content = full_json_str[i:i + chunk_size]
                            # 流式发送 JSON 字符
                            yield create_chunk(content=chunk_content)
                            await asyncio.sleep(0.01) # 微小延迟确保顺序

                        # 🎯 核心修复 3：单独发送 data 变量，不要和巨大的 content 挤在一个包里
                        yield create_chunk(content="\n\n✅ **数据同步完成**", data=cache['data'], finish=True)
                        yield "data: [DONE]\n\n"
                        return
            except Exception as e:
                logger.error(f"缓存读取失败: {e}")

        # 3. 真实 RPA 抓取流程 (逻辑同上，但在最终输出处也使用分段传输)
        async with async_playwright() as p:
            try:
                browser, page = await get_browser_context(p)
                automator = PofficesAutomator(page)
                yield create_chunk(content="🚀 **正在执行自动化抓取...**\n")
                
                await page.goto(BASE_URL, timeout=90000, wait_until="commit")
                await automator.run_detailed_rpa(query)
                
                capture_task = asyncio.create_task(automator.wait_and_capture_assets())
                while not capture_task.done():
                    yield create_chunk(content=".") 
                    await asyncio.sleep(5)
                
                extracted_data = await capture_task
                
                if extracted_data:
                    # 保存物理文件时保持 indent=4
                    with open(CACHE_PATH, 'w', encoding='utf-8') as f:
                        json.dump({"query": query, "data": extracted_data}, f, indent=4, ensure_ascii=False)
                    
                    # 同样执行分段输出逻辑
                    final_json_str = json.dumps(extracted_data, indent=4, ensure_ascii=False)
                    for i in range(0, len(final_json_str), 2000):
                        yield create_chunk(content=final_json_str[i:i+2000])
                    
                    yield create_chunk(content="\n\n✅ **实时抓取完成**", data=extracted_data, finish=True)
                else:
                    yield create_chunk(content="\n❌ 内容为空")
            except Exception as e:
                yield create_chunk(content=f"\n❌ 错误: {str(e)}")
            finally:
                if 'browser' in locals(): await browser.close()
        
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
# [下方的审计功能 run_f2 和 run_f3 保持不变...]

# ==========================================
# 4. 审计功能部分
# ==========================================

async def run_f2_text_audit(request: Request):
    body = await request.json()
    query, data = extract_input_data(body)
    if not data or not data.get('text'):
        return {"choices": [{"message": {"content": "⚠️ 未接收到文本。"}}] }
    score = TextAuditor(_sbert).audit(query, data['text'], SCORING_CONFIG['text_hybrid'])
    return {"choices": [{"message": {"content": f"### 📝 文本审计: `{score}/10`"}}]}

async def run_f3_multimodal_audit(request: Request):
    body = await request.json()
    query, data = extract_input_data(body)
    if not data: return {"choices": [{"message": {"content": "⚠️ 未接收到数据。"}}] }
    t_score = TextAuditor(_sbert).audit(query, data.get('text', ''), SCORING_CONFIG['text_hybrid'])
    i_score = await ImageAuditor(_clip_m, _clip_p).audit(query, data.get('images', []))
    msg = f"### 🔍 综合审计\n- 文本: `{t_score}`\n- 图片: `{i_score or 'N/A'}`"
    return {"choices": [{"message": {"content": msg}}]}