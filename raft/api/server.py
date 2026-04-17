"""FastAPI HTTP 服务：统一 Block API 信封，供 Postman 或 /docs 测试。B9 默认使用真实 RPA（Playwright），未安装则 Mock。"""
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent.parent
try:
    from dotenv import load_dotenv

    _env_file = _BASE / ".env"
    if _env_file.is_file():
        load_dotenv(_env_file)
except ImportError:
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from raft.contracts.api import ApiError, BlockRequest, BlockResponse
from raft.contracts.models import ExperimentConfig, TaskSpec
from raft.core.config.loader import b1_load_config, load_experiment_config, load_task_spec
from raft.orchestrator.runner import Orchestrator
from raft.evaluation.metrics import evaluate_trajectory
from raft.api.poffices_router import router as poffices_router

# 路径基准：默认项目根（启动目录），可通过环境变量 RAFT_BASE_DIR 覆盖
BASE_DIR = _BASE  # 与上方 _BASE 一致；启动时已尝试 load_dotenv(BASE_DIR / ".env")

app = FastAPI(
    title="RAFT Block API",
    description="RPA-Augmented Testing Framework — 统一 Block 请求/响应，可用 Postman 或 /docs 测试",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Poffices Custom Block 专用路由（不依赖 Playwright，可独立部署）
app.include_router(poffices_router)


def _resolve_path(path: str) -> Path:
    """将 payload 中的相对路径解析为基于 BASE_DIR 的绝对路径。"""
    p = Path(path)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p


@app.get("/")
def root() -> dict:
    return {
        "service": "RAFT Block API",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/health",
        "blocks": {
            "B1": "POST /api/v1/b1/load_config",
            "B8": "POST /api/v1/b8/evaluate",
            "B9": "POST /api/v1/b9/run",
            "Poffices-Planner": "POST /api/v1/poffices/plan  (Custom Block 规划入口，仅返回规划步骤)",
            "Poffices-Run":     "POST /api/v1/poffices/run   (Custom Block 完整执行：规划+RPA+测试结果)",
        },
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "base_dir": str(BASE_DIR)}


# ---------- B1: 加载实验配置与任务规范 ----------


@app.post("/api/v1/b1/load_config", response_model=BlockResponse)
def api_b1_load_config(request: BlockRequest) -> BlockResponse:
    """
    B1：根据 payload 加载 ExperimentConfig 和/或 TaskSpec。
    payload 中路径可为相对路径（相对于服务启动时的项目根）。
    """
    req = request.model_copy(deep=True)
    if req.payload.get("config_path"):
        req.payload["config_path"] = str(_resolve_path(req.payload["config_path"]))
    if req.payload.get("task_spec_path"):
        req.payload["task_spec_path"] = str(_resolve_path(req.payload["task_spec_path"]))
    return b1_load_config(req)


# ---------- B8: 评估单条轨迹（供其他平台提交轨迹拿报告） ----------


@app.post("/api/v1/b8/evaluate")
def api_b8_evaluate(body: dict) -> dict:
    """
    B8：对一条轨迹做评估，返回 RunMetrics（success, step_count, run_id, details）。
    请求体：{ "trajectory": [...], "task_spec": {...}, "run_id": "可选" }。
    供其他平台跑完真实 Agent 后，把 trajectory + task_spec 发过来拿评估结果。
    """
    trajectory = body.get("trajectory", [])
    task_spec = body.get("task_spec", {})
    run_id = body.get("run_id")
    task = TaskSpec.model_validate(task_spec)
    metrics = evaluate_trajectory(trajectory, task, run_id=run_id)
    return {"code": "ok", "data": metrics.model_dump(), "error": None}


# ---------- B9: Orchestrator 跑一轮任务 ----------


@app.post("/api/v1/b9/run")
def api_b9_run(request: BlockRequest) -> BlockResponse:
    """
    B9：跑一轮闭环 1（加载 Config + TaskSpec → 多步执行 → 返回轨迹与验收结果）。
    payload 二选一：
    - config_path + task_spec_path + task_spec_id：从文件加载；
    - experiment_config + task_spec：直接传对象（dict）。
    可选：max_steps（默认 5）。
    """
    req_id = request.request_id
    block_id = request.block_id or "B9"
    payload = request.payload
    max_steps = int(payload.get("max_steps", 5))

    try:
        if "experiment_config" in payload and "task_spec" in payload:
            config = ExperimentConfig.model_validate(payload["experiment_config"])
            task = TaskSpec.model_validate(payload["task_spec"])
        elif "config_path" in payload and "task_spec_path" in payload and "task_spec_id" in payload:
            config_path = str(_resolve_path(payload["config_path"]))
            task_path = str(_resolve_path(payload["task_spec_path"]))
            config = load_experiment_config(config_path)
            task = load_task_spec(task_path, payload["task_spec_id"])
        else:
            return BlockResponse(
                request_id=req_id,
                block_id=block_id,
                code="invalid_payload",
                data=None,
                error=ApiError(
                    code="invalid_payload",
                    message="payload 需包含 (config_path, task_spec_path, task_spec_id) 或 (experiment_config, task_spec)",
                    details=payload,
                ),
            )

        orch = Orchestrator(max_steps=max_steps)
        try:
            result = orch.run_until_done(config, task)
        finally:
            if hasattr(orch.rpa, "close") and callable(getattr(orch.rpa, "close")):
                orch.rpa.close()
        return BlockResponse(
            request_id=req_id,
            block_id=block_id,
            code="ok",
            data=result,
            error=None,
        )
    except FileNotFoundError as e:
        return BlockResponse(
            request_id=req_id,
            block_id=block_id,
            code="config_not_found",
            data=None,
            error=ApiError(code="config_not_found", message=str(e), details=payload),
        )
    except ValueError as e:
        return BlockResponse(
            request_id=req_id,
            block_id=block_id,
            code="task_spec_not_found",
            data=None,
            error=ApiError(code="task_spec_not_found", message=str(e), details=payload),
        )
    except Exception as e:
        return BlockResponse(
            request_id=req_id,
            block_id=block_id,
            code="B9.internal_error",
            data=None,
            error=ApiError(code="B9.internal_error", message=str(e), details={}),
        )
