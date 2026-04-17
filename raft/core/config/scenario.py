"""ScenarioSpec 加载、解析与运行时取值辅助。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from raft.contracts.models import ExperimentConfig, ScenarioSpec, TaskSpec


def load_scenario_spec(spec_path: str | Path) -> ScenarioSpec:
    """从 JSON 文件加载场景规范。"""
    path = Path(spec_path)
    if not path.exists():
        raise FileNotFoundError(f"Scenario spec file not found: {path}")
    text = path.read_text(encoding="utf-8")
    return ScenarioSpec.model_validate(json.loads(text))


def resolve_scenario_spec(
    config_path: str | Path,
    raw_data: dict[str, Any],
) -> tuple[ScenarioSpec | None, str | None]:
    """
    根据实验配置解析场景规范。

    优先级：
    1. inline scenario_spec
    2. scenario_spec_path
    3. scenario_id / scenario 对应的同目录 `<id>.json`
    """
    inline_spec = raw_data.get("scenario_spec")
    if isinstance(inline_spec, dict):
        spec = ScenarioSpec.model_validate(inline_spec)
        return (spec, raw_data.get("scenario_spec_path"))

    base_dir = Path(config_path).resolve().parent
    explicit_path = raw_data.get("scenario_spec_path")
    if isinstance(explicit_path, str) and explicit_path.strip():
        candidate = Path(explicit_path)
        if not candidate.is_absolute():
            candidate = base_dir / explicit_path
        spec = load_scenario_spec(candidate)
        return (spec, str(candidate))

    scenario_id = raw_data.get("scenario_id") or raw_data.get("scenario")
    if isinstance(scenario_id, str) and scenario_id.strip():
        candidate = base_dir / f"{scenario_id.strip()}.json"
        if candidate.exists():
            spec = load_scenario_spec(candidate)
            return (spec, str(candidate))

    return (None, None)


def get_scenario_spec(config: ExperimentConfig) -> ScenarioSpec | None:
    spec = getattr(config, "scenario_spec", None)
    return spec if isinstance(spec, ScenarioSpec) else None


def resolve_scenario_label(config: ExperimentConfig) -> str:
    spec = get_scenario_spec(config)
    return (
        (spec.id if spec else None)
        or getattr(config, "scenario_id", None)
        or getattr(config, "scenario", "")
        or ""
    )


def resolve_scenario_prompt(config: ExperimentConfig) -> str:
    """构造给 LLM / 报告使用的场景描述文本。"""
    spec = get_scenario_spec(config)
    if spec is None:
        return resolve_scenario_label(config)

    parts: list[str] = [f"场景 ID：{spec.id}"]
    if spec.name:
        parts.append(f"场景名称：{spec.name}")
    narrative = spec.narrative or spec.description
    if narrative:
        parts.append(f"场景描述：{narrative}")
    if spec.allowed_agents:
        parts.append(f"允许 Agent：{', '.join(spec.allowed_agents)}")
    if spec.allowed_blocks:
        block_ids = [
            item.get("block_id")
            for item in spec.allowed_blocks
            if isinstance(item, dict) and isinstance(item.get("block_id"), str)
        ]
        if block_ids:
            parts.append(f"允许 Block：{', '.join(block_ids)}")
    if spec.flow_template:
        tmpl = spec.flow_template
        if tmpl.description:
            parts.append(f"推荐流程：{tmpl.description}")
        template_steps = [
            step.get("block_id") or step.get("tool_name")
            for step in tmpl.steps
            if isinstance(step, dict)
        ]
        template_steps = [str(step) for step in template_steps if step]
        if template_steps:
            parts.append(f"流程模板步骤：{' -> '.join(template_steps)}")
    if spec.constraints:
        if spec.constraints.required_blocks:
            parts.append(f"必须包含：{', '.join(spec.constraints.required_blocks)}")
        if spec.constraints.forbidden_blocks:
            parts.append(f"禁止使用：{', '.join(spec.constraints.forbidden_blocks)}")
        if spec.constraints.notes:
            parts.append(f"其他约束：{'；'.join(spec.constraints.notes)}")
    return "\n".join(parts)


def resolve_block_semantics_for_planner(config: ExperimentConfig) -> str:
    """
    从 scenario 的 block_semantics 构建供 LLM planner 阅读的 block 语义文本。
    若 block_semantics 不存在，则从 allowed_blocks 生成简要说明。
    """
    spec = get_scenario_spec(config)
    if spec is None:
        return ""
    semantics = getattr(spec, "block_semantics", None) or {}
    if not isinstance(semantics, dict):
        return ""

    flow_types = semantics.get("flow_types") or {}
    blocks = semantics.get("blocks") or []

    parts: list[str] = []

    if flow_types:
        parts.append("## 流程类型（三选一，不可混用）")
        for ft_id, ft in flow_types.items():
            if isinstance(ft, dict):
                desc = ft.get("description", "")
                steps = ft.get("steps", "")
                when = ft.get("when", "")
                parts.append(f"- **{ft_id}**: {desc}")
                parts.append(f"  步骤: {steps}")
                if when:
                    parts.append(f"  适用: {when}")

    if blocks:
        parts.append("\n## Block 详细语义")
        for b in blocks:
            if not isinstance(b, dict) or not b.get("block_id"):
                continue
            bid = b.get("block_id", "")
            parts.append(f"\n### {bid}")
            parts.append(f"- 描述: {b.get('description', '')}")
            if b.get("flow_type"):
                parts.append(f"- 流程归属: {b['flow_type']}")
            if b.get("semantic_detail"):
                parts.append(f"- 详细说明: {b['semantic_detail']}")
            if b.get("precondition"):
                parts.append(f"- 前置条件: {b['precondition']}")
            if b.get("side_effect"):
                parts.append(f"- 副作用: {b['side_effect']}")
            if b.get("do_not_use_in"):
                parts.append(f"- 与以下流程语义不兼容（勿混用）: {b['do_not_use_in']}")
            if b.get("must_follow_with"):
                parts.append(f"- 必须紧跟: {b['must_follow_with']}")
            if b.get("do_not_insert_between"):
                parts.append(f"- 与以下 block 之间不得插入其他步骤: {b['do_not_insert_between']}")
            if b.get("use_with_caution"):
                parts.append(f"- 【慎用】主流程规划中不要使用，仅用于失败恢复")
            if b.get("after_refresh_must"):
                parts.append(f"- 刷新后必须: {b['after_refresh_must']}")
            params = b.get("params")
            if params is not None:
                parts.append(f"- params: {json.dumps(params, ensure_ascii=False)}")

    # 复合 Block：作为可选快捷方式，LLM 可根据 goal 选择调用或使用基础 block 组合
    compound_blocks = getattr(spec, "compound_blocks", None)
    if isinstance(compound_blocks, list) and compound_blocks:
        parts.append("\n## 复合 Block（可选快捷方式，符合时可直接调用；否则用基础 block 组合）")
        for cb in compound_blocks:
            if isinstance(cb, dict) and cb.get("block_id"):
                desc = cb.get("description", "")
                params = cb.get("params_schema")
                params_str = f" params: {json.dumps(params, ensure_ascii=False)}" if params else ""
                parts.append(f"- **{cb['block_id']}**: {desc}{params_str}")

    if not parts:
        # 回退：从 allowed_blocks 生成简要列表
        if spec.allowed_blocks:
            block_ids = [
                item.get("block_id")
                for item in spec.allowed_blocks
                if isinstance(item, dict) and item.get("block_id")
            ]
            if block_ids:
                parts.append(f"可用 blocks: {', '.join(block_ids)}")

    return "\n".join(parts) if parts else ""


def resolve_block_catalog(config: ExperimentConfig) -> list[dict[str, Any]] | None:
    spec = get_scenario_spec(config)
    if spec and spec.allowed_blocks:
        return [dict(item) for item in spec.allowed_blocks if isinstance(item, dict)]
    extra = getattr(config, "extra", None) or {}
    raw = extra.get("block_catalog")
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    return None


def resolve_allowed_agents(config: ExperimentConfig) -> list[str]:
    spec = get_scenario_spec(config)
    if spec and spec.allowed_agents:
        return [item for item in spec.allowed_agents if isinstance(item, str) and item.strip()]
    extra = getattr(config, "extra", None) or {}
    raw = extra.get("available_agents")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str) and item.strip()]
    return []


def resolve_suggested_agents(config: ExperimentConfig) -> list[str]:
    spec = get_scenario_spec(config)
    if spec and spec.suggested_agents:
        return [item for item in spec.suggested_agents if isinstance(item, str) and item.strip()]
    if spec and spec.allowed_agents:
        return [item for item in spec.allowed_agents if isinstance(item, str) and item.strip()]
    return []


def resolve_compound_blocks(config: ExperimentConfig) -> list[dict[str, Any]]:
    """解析场景中的复合 Block 列表，供 LLM planner 选择调用。LLM 主导：根据 goal 选择复合 Block 或基础 block 组合流程。"""
    spec = get_scenario_spec(config)
    if spec and getattr(spec, "compound_blocks", None):
        return [dict(item) for item in spec.compound_blocks if isinstance(item, dict)]
    return []


def resolve_flow_template(config: ExperimentConfig) -> dict[str, Any] | None:
    spec = get_scenario_spec(config)
    if spec and spec.flow_template:
        return spec.flow_template.model_dump()
    return None


def resolve_constraints(config: ExperimentConfig) -> dict[str, Any] | None:
    spec = get_scenario_spec(config)
    if spec and spec.constraints:
        return spec.constraints.model_dump()
    return None


def resolve_planner_hints(config: ExperimentConfig) -> dict[str, Any]:
    """从场景规范与实验 extra 中解析规划器提示配置。"""
    hints: dict[str, Any] = {}
    spec = get_scenario_spec(config)
    if spec and isinstance(spec.extra, dict):
        planner_cfg = spec.extra.get("planner")
        if isinstance(planner_cfg, dict):
            hints.update(planner_cfg)
    extra = getattr(config, "extra", None) or {}
    planner_override = extra.get("planner")
    if isinstance(planner_override, dict):
        hints.update(planner_override)
    # 向后兼容：experiment.extra.planner_ignore_template 覆盖 use_template_as_hint
    if "planner_ignore_template" in extra:
        try:
            ignore = bool(extra.get("planner_ignore_template"))
            hints["use_template_as_hint"] = not ignore
        except Exception:
            pass
    return hints


def resolve_default_task_description(config: ExperimentConfig, task_spec: TaskSpec) -> str:
    if task_spec.description:
        return task_spec.description
    spec = get_scenario_spec(config)
    if spec:
        return spec.description or spec.narrative or resolve_scenario_label(config)
    return resolve_scenario_label(config)


def validate_scenario_run(config: ExperimentConfig, task_spec: TaskSpec) -> None:
    """在执行前检查任务/Agent/Block 是否满足场景规范。"""
    spec = get_scenario_spec(config)
    if spec is None:
        return

    if spec.task_spec_ids and task_spec.task_spec_id not in spec.task_spec_ids:
        raise ValueError(
            f"TaskSpec '{task_spec.task_spec_id}' is not allowed in scenario '{spec.id}'"
        )

    extra = getattr(config, "extra", None) or {}
    allowed_agents = {item.strip() for item in spec.allowed_agents if isinstance(item, str) and item.strip()}
    # Discovery 从 UI 动态发现的 agents 视为允许（不依赖 scenario 预置列表）
    discovery_agents = extra.get("agents_from_discovery") or extra.get("discovery_agents")
    if isinstance(discovery_agents, list):
        allowed_agents = allowed_agents | {a.strip() for a in discovery_agents if isinstance(a, str) and a.strip()}
    if allowed_agents:
        agent_under_test = (extra.get("agent_under_test") or "").strip() if isinstance(extra.get("agent_under_test"), str) else ""
        if agent_under_test and agent_under_test not in allowed_agents:
            raise ValueError(
                f"Agent '{agent_under_test}' is not allowed in scenario '{spec.id}'"
            )
        agents_to_test = extra.get("agents_to_test")
        if isinstance(agents_to_test, list):
            invalid = [
                agent for agent in agents_to_test
                if isinstance(agent, str) and agent.strip() and agent.strip() not in allowed_agents
            ]
            if invalid:
                raise ValueError(
                    f"Agents {invalid} are not allowed in scenario '{spec.id}'"
                )

    block_catalog = resolve_block_catalog(config) or []
    block_ids = {
        item.get("block_id")
        for item in block_catalog
        if isinstance(item, dict) and isinstance(item.get("block_id"), str)
    }
    constraints = spec.constraints
    if constraints:
        missing_required = [item for item in constraints.required_blocks if item not in block_ids]
        if missing_required:
            raise ValueError(
                f"Scenario '{spec.id}' requires blocks {missing_required}, but they are missing"
            )
        forbidden = [item for item in constraints.forbidden_blocks if item in block_ids]
        if forbidden:
            raise ValueError(
                f"Scenario '{spec.id}' forbids blocks {forbidden}, but they are configured"
            )
