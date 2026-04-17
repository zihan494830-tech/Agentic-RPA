"""B1: 至少解析一种 Config/TaskSpec 格式。"""
import json
import tempfile
from pathlib import Path

import pytest

from raft.core.config.loader import load_experiment_config, load_task_spec
from raft.core.config.loader import b1_load_config
from raft.contracts.api import BlockRequest


def test_load_experiment_config_from_json() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({
            "experiment_id": "exp-1",
            "scenario": "demo",
            "task_spec_ids": ["t1"],
        }, f, ensure_ascii=False)
        path = f.name
    try:
        config = load_experiment_config(path)
        assert config.experiment_id == "exp-1"
        assert config.scenario == "demo"
        assert config.task_spec_ids == ["t1"]
    finally:
        Path(path).unlink(missing_ok=True)


def test_load_experiment_config_resolves_scenario_spec() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        scenario_path = base / "demo.json"
        scenario_path.write_text(json.dumps({
            "id": "demo",
            "description": "场景描述",
            "allowed_agents": ["Agent A"],
            "allowed_blocks": [{"block_id": "app_ready", "params": {}}],
        }, ensure_ascii=False), encoding="utf-8")
        config_path = base / "experiment.json"
        config_path.write_text(json.dumps({
            "experiment_id": "exp-1",
            "scenario": "demo",
            "task_spec_ids": ["t1"],
        }, ensure_ascii=False), encoding="utf-8")

        config = load_experiment_config(config_path)
        assert config.scenario_id == "demo"
        assert config.scenario_spec is not None
        assert config.scenario_spec.id == "demo"
        assert config.scenario_spec.allowed_agents == ["Agent A"]
        assert config.scenario_spec_path is not None


def test_load_task_spec_from_json() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({
            "task_specs": [
                {"task_spec_id": "t1", "description": "desc", "initial_state": {}},
            ]
        }, f, ensure_ascii=False)
        path = f.name
    try:
        spec = load_task_spec(path, "t1")
        assert spec.task_spec_id == "t1"
        assert spec.description == "desc"
    finally:
        Path(path).unlink(missing_ok=True)


def test_b1_block_api_ok() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"experiment_id": "e1", "scenario": "s1", "task_spec_ids": []}, f)
        config_path = f.name
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as g:
        json.dump({"task_specs": [{"task_spec_id": "t1", "description": ""}]}, g)
        task_path = g.name
    try:
        req = BlockRequest(
            request_id="r1",
            block_id="B1",
            api_version="v1",
            payload={"config_path": config_path, "task_spec_path": task_path, "task_spec_id": "t1"},
        )
        resp = b1_load_config(req)
        assert resp.code == "ok"
        assert "experiment_config" in (resp.data or {})
        assert "task_spec" in (resp.data or {})
    finally:
        Path(config_path).unlink(missing_ok=True)
        Path(task_path).unlink(missing_ok=True)
