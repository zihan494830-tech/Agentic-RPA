"""B1: 从文件加载 ExperimentConfig 与 TaskSpec，至少支持一种格式（JSON）。"""
import json
from pathlib import Path

from raft.contracts.api import ApiError, BlockRequest, BlockResponse
from raft.contracts.models import ExperimentConfig, TaskSpec
from raft.core.config.scenario import resolve_scenario_spec


def _read_json(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def load_experiment_config(config_path: str | Path) -> ExperimentConfig:
    """从 JSON 文件加载实验配置。"""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Experiment config file not found: {path}")
    data = _read_json(path)
    spec, resolved_path = resolve_scenario_spec(path, data)
    if spec is not None:
        data = dict(data)
        data["scenario_spec"] = spec.model_dump()
        data["scenario_id"] = data.get("scenario_id") or spec.id
        data["scenario_spec_path"] = data.get("scenario_spec_path") or resolved_path
    return ExperimentConfig.model_validate(data)


def load_task_spec(
    config_path: str | Path,
    task_spec_id: str,
) -> TaskSpec:
    """从 JSON 文件加载指定 task_spec_id 的任务规范。
    文件格式为：{ "task_specs": [ { "task_spec_id": "...", ... }, ... ] } 或单条 { "task_spec_id": "...", ... }。
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Task spec file not found: {path}")
    data = _read_json(path)
    if "task_specs" in data:
        for spec in data["task_specs"]:
            if spec.get("task_spec_id") == task_spec_id:
                return TaskSpec.model_validate(spec)
        raise ValueError(f"TaskSpec id not found: {task_spec_id}")
    # 单条
    if data.get("task_spec_id") == task_spec_id:
        return TaskSpec.model_validate(data)
    raise ValueError(f"TaskSpec id not found: {task_spec_id}")


def b1_load_config(request: BlockRequest) -> BlockResponse:
    """B1 统一 API：根据 payload 加载 ExperimentConfig 和/或 TaskSpec。"""
    req_id = request.request_id
    block_id = request.block_id or "B1"
    payload = request.payload
    try:
        result: dict = {}
        if "config_path" in payload:
            config = load_experiment_config(payload["config_path"])
            result["experiment_config"] = config.model_dump()
        if "task_spec_path" in payload and "task_spec_id" in payload:
            task = load_task_spec(payload["task_spec_path"], payload["task_spec_id"])
            result["task_spec"] = task.model_dump()
        if not result:
            return BlockResponse(
                request_id=req_id,
                block_id=block_id,
                code="invalid_payload",
                data=None,
                error=ApiError(
                    code="invalid_payload",
                    message="payload must contain config_path and/or (task_spec_path, task_spec_id)",
                    details=payload,
                ),
            )
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
            error=ApiError(
                code="config_not_found",
                message=str(e),
                details={"path": str(payload.get("config_path") or payload.get("task_spec_path"))},
            ),
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
            code="B1.internal_error",
            data=None,
            error=ApiError(
                code="B1.internal_error",
                message=str(e),
                details={},
            ),
        )
