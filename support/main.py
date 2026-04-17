import sys
import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# 🎯 导入我们在 pipeline.py 中定义的三个核心功能函数
from .pipeline import run_f1_rpa, run_f2_text_audit, run_f3_multimodal_audit

# 配置标准输出编码为 UTF-8，防止日志乱码
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# 配置全局日志
logging.basicConfig(
    level=logging.INFO, 
    format='[%(asctime)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="GenAI Agent Performance Data Analyzer")

# 🎯 配置 CORS 跨域，确保 Poffices 平台可以顺利调用接口
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    """健康检查接口"""
    return {
        "status": "Running", 
        "message": "Performance Data Analyzer Gateway is Ready"
    }

# ============================================================
# 功能一：RPA 模拟操作接口
# 对应 Poffices Custom Block 1: 负责登录、选 Agent、提交指令
# ============================================================
@app.post("/rpa/operate")
async def rpa_operate(request: Request):
    logger.info(">>> 收到功能一请求：执行 RPA 模拟操作")
    return await run_f1_rpa(request)

# ============================================================
# 功能二：内容相关性评估接口
# 对应 Poffices Custom Block 2: 负责抓取文本并进行 TF-IDF/SBERT 评分
# ============================================================
@app.post("/analyze/text")
async def analyze_text(request: Request):
    logger.info(">>> 收到功能二请求：执行文本相关性审计")
    return await run_f2_text_audit(request)

# ============================================================
# 功能三：多模态综合评分接口
# 对应 Poffices Custom Block 3: 负责图片、文献及文本的自适应加权评分
# ============================================================
@app.post("/analyze/full")
async def analyze_full(request: Request):
    logger.info(">>> 收到功能三请求：执行多模态综合评分审计")
    return await run_f3_multimodal_audit(request)

if __name__ == "__main__":
    import uvicorn
    # 本地调试运行：python -m api.main
    uvicorn.run(app, host="0.0.0.0", port=18888)