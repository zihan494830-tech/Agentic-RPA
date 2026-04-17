"""业务契约：ExperimentConfig、TaskSpec、ExecutionResult、TrajectoryEntry、StepResult 等。"""
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------- B2/B3/B4：路由与编排 ----------

RouteType = Literal["single_flow", "multi_flow"]
"""B2 输出：单流线性 vs 多流（可分支/汇聚）。"""

AgentRole = Literal["planner", "execution", "verifier"]
"""B4 输出：步骤对应的 Agent 角色。"""

ExecutionErrorType = Literal[
    "timeout",
    "unknown_tool",
    "rpa_execution_failed",
    "validation_error",
    "element_not_found",
    "missing_context",
]
"""B7 常见失败类型约定；兼容历史数据时仍允许自定义字符串。"""


class DifficultyRoutingResult(BaseModel):
    """B2 输出：难度、路由类型及建议测试轮数（可来自规则映射或 LLM 一并输出）。"""
    route_type: RouteType = Field(..., description="single_flow 或 multi_flow")
    difficulty: float = Field(default=0.0, description="难度估计 [0,1]，可选")
    suggested_rounds: int | None = Field(default=None, description="建议多轮测试轮数 2～6，由 B2 规则或 LLM 填充")
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展字段")


class WorkflowDAG(BaseModel):
    """B3 输出：步骤 DAG，节点为步序号，边为依赖。"""
    nodes: list[int] = Field(..., description="节点即步序号 0..n-1")
    edges: list[tuple[int, int]] = Field(default_factory=list, description="(from_step, to_step) 依赖")
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展字段")


class StepAssignment(BaseModel):
    """B4 输出：当前步对应的 Agent 与工具目标。"""
    step_index: int = Field(..., description="步序号")
    agent_role: AgentRole = Field(..., description="planner / execution / verifier")
    tool_target: Literal["rpa", "api"] = Field(default="rpa", description="工具目标")
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展字段")


# ---------- B1 实验与任务 ----------


class FaultInjectionConfig(BaseModel):
    """B7 故障注入配置（鲁棒性/压力测试）。"""
    delay_prob: float = Field(default=0.0, description="随机延迟概率 [0,1]")
    delay_sec_range: tuple[float, float] = Field(default=(0.5, 2.0), description="延迟秒数范围")
    error_prob: float = Field(default=0.0, description="随机返回失败的概率 [0,1]")
    missing_element_steps: list[int] = Field(default_factory=list, description="在这些步数强制返回 element_not_found")
    missing_element_step_ids: list[str] = Field(default_factory=list, description="在这些 step_id 强制返回 element_not_found")
    timeout_steps: list[int] = Field(default_factory=list, description="在这些步数强制返回 timeout")
    timeout_step_ids: list[str] = Field(default_factory=list, description="在这些 step_id 强制返回 timeout")
    seed: int | None = Field(default=None, description="随机种子，便于复现")


class RPAConfig(BaseModel):
    """场景级 RPA 配置（正常/鲁棒性/压力）。"""
    mode: Literal["normal", "robustness", "stress"] = Field(default="normal", description="normal=无注入；robustness=轻度故障；stress=重度")
    fault_injection: FaultInjectionConfig | None = Field(default=None, description="故障注入参数；为 None 时由 mode 使用默认")
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展字段")


class ScenarioFlowTemplate(BaseModel):
    """场景推荐流程模板：用于约束或提示规划器的默认执行形态。"""
    template_id: str = Field(default="", description="模板 ID")
    description: str = Field(default="", description="模板说明")
    steps: list[dict[str, Any]] = Field(
        default_factory=list,
        description="推荐步骤列表；每步至少可含 block_id/tool_name、params",
    )
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展字段")


class ScenarioConstraints(BaseModel):
    """场景约束：用于限制规划器与执行器可使用的能力。"""
    required_blocks: list[str] = Field(default_factory=list, description="必须可用/应包含的 Block")
    forbidden_blocks: list[str] = Field(default_factory=list, description="禁止使用的 Block")
    notes: list[str] = Field(default_factory=list, description="补充约束说明")
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展字段")


class ScenarioSpec(BaseModel):
    """场景规范：描述场景允许的 Agent/Block、推荐流程与约束。"""
    id: str = Field(..., description="场景唯一标识")
    name: str = Field(default="", description="场景名称")
    description: str = Field(default="", description="场景描述/任务描述来源")
    narrative: str = Field(default="", description="供 LLM 使用的场景叙述")
    task_spec_ids: list[str] = Field(default_factory=list, description="本场景允许的 TaskSpec ID 列表")
    allowed_agents: list[str] = Field(default_factory=list, description="本场景允许的 Agent 列表")
    suggested_agents: list[str] = Field(default_factory=list, description="本场景建议优先测试的 Agent 列表")
    allowed_blocks: list[dict[str, Any]] = Field(default_factory=list, description="本场景允许的 Block 集合")
    compound_blocks: list[dict[str, Any]] = Field(
        default_factory=list,
        description="复合 Block：已验证的 RPA 流程，可被 planner 调用并展开为原子步骤。含 block_id、description、params_schema、steps/step_template、iterate 等。",
    )
    flow_template: ScenarioFlowTemplate | None = Field(default=None, description="推荐流程模板")
    constraints: ScenarioConstraints | None = Field(default=None, description="场景约束")
    block_semantics: dict[str, Any] | None = Field(
        default=None,
        description="Block 语义详细定义（flow_types、blocks），供 LLM planner 理解每个 block 的用途、副作用与流程归属",
    )
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展字段")


class ExperimentConfig(BaseModel):
    """实验配置：场景/任务/GT 定义。"""
    experiment_id: str = Field(..., description="实验标识")
    scenario: str = Field(default="", description="场景名称")
    scenario_id: str | None = Field(default=None, description="显式场景 ID，可用于定位场景规范")
    scenario_spec_path: str | None = Field(default=None, description="场景规范文件路径")
    scenario_spec: ScenarioSpec | None = Field(default=None, description="已解析的场景规范")
    task_spec_ids: list[str] = Field(default_factory=list, description="本实验包含的 TaskSpec ID 列表")
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展字段；可含 rpa_config: RPAConfig 的 dict 形态")


class RuleCriteriaConfig(BaseModel):
    """规则型判据配置：与 LLM 分数一起写进报告。"""
    required_tool_calls: list[str] = Field(default_factory=list, description="轨迹中必须出现过的工具名（至少各出现一次）")
    required_step_success: list[int] = Field(default_factory=list, description="这些步序号必须全部 execution 成功")
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展规则")


class TaskSpec(BaseModel):
    """任务规范：描述、初始状态、Ground Truth。"""
    task_spec_id: str = Field(..., description="任务规范 ID")
    description: str = Field(default="", description="任务描述")
    initial_state: dict[str, Any] = Field(default_factory=dict, description="初始状态")
    ground_truth: dict[str, Any] | None = Field(default=None, description="Ground Truth，可简化为关键字段或规则")
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展字段；可含 rule_criteria: RuleCriteriaConfig 的 dict 形态")


# ---------- B7 执行反馈（闭环 1 必需） ----------


class ExecutionResult(BaseModel):
    """RPA/工具执行统一反馈，供闭环 1（RPA→Agent）使用。"""
    success: bool = Field(..., description="是否执行成功")
    error_type: ExecutionErrorType | str | None = Field(
        default=None,
        description=(
            "失败时错误类型。推荐使用 timeout/unknown_tool/rpa_execution_failed/"
            "validation_error/element_not_found/missing_context。"
        ),
    )
    raw_response: str | dict[str, Any] | None = Field(default=None, description="原始响应或消息")
    output_text: str | None = Field(default=None, description="标准化文本产出，供 gate / 评估统一读取")
    ui_state_delta: dict[str, Any] | None = Field(
        default=None,
        description=(
            "本次执行产生的 UI / 状态增量，格式为 {key: value}。"
            "B5 StateManager 与 B8 评估器均按「顺序覆盖」语义合并多个 execution 的 delta："
            "同一 key 后者覆盖前者，因此各 execution 应使用不相交的 key 以避免歧义。"
            "典型 key：response_text、poffices_response、final_report、agent_name 等。"
        ),
    )
    tool_name: str | None = Field(default=None, description="调用的工具名")
    step_id: str | None = Field(default=None, description="对应的计划 step_id（若存在）")
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展字段")


# ---------- 轨迹与步骤 ----------


class ToolCall(BaseModel):
    """单次工具调用请求。"""
    tool_name: str = Field(..., description="工具名")
    params: dict[str, Any] = Field(default_factory=dict, description="参数")
    step_id: str | None = Field(default=None, description="对应 GoalPlanStep.step_id；运行时注入，便于审计与故障复现")


class GoalPlanStep(BaseModel):
    """目标驱动计划中的单个步骤。"""
    step_id: str = Field(..., description="步骤唯一标识（同一 plan 内唯一）")
    tool_call: ToolCall = Field(..., description="本步骤要执行的工具调用")
    depends_on: list[str] = Field(default_factory=list, description="本步骤依赖的前置步骤 step_id 列表")
    note: str | None = Field(default=None, description="步骤备注（可选）")
    expected_output: str | None = Field(default=None, description="本步期望产出的描述，供 gate 验收使用")
    gate: Literal["none", "auto", "human"] = Field(default="none", description="审核类型：none=直接通过，auto=规则自动校验，human=人工确认")
    risk_level: Literal["low", "medium", "high"] = Field(default="low", description="风险等级，影响执行策略与 gate 选择")


class GoalPlan(BaseModel):
    """目标驱动执行计划：步骤 + 来源。"""
    steps: list[GoalPlanStep] = Field(default_factory=list, description="计划步骤（可含依赖关系）")
    source: Literal["llm", "rule_fallback", "compound_block", "replan_rule", "replan_llm"] = Field(
        default="rule_fallback",
        description="计划来源",
    )
    reason: str | None = Field(default=None, description="计划生成原因（如失败重规划）")


class StepResult(BaseModel):
    """单步执行结果：工具调用 + 执行反馈。"""
    step_index: int = Field(..., description="步序号")
    tool_calls: list[ToolCall] = Field(default_factory=list, description="本步发出的工具调用")
    execution_results: list[ExecutionResult] = Field(default_factory=list, description="对应执行结果")
    agent_input_snapshot: dict[str, Any] | None = Field(default=None, description="本步 Agent 输入摘要（含最近 ExecutionResult）")


class TrajectoryEntry(BaseModel):
    """轨迹一条记录：可序列化并写回。"""
    step_index: int = Field(..., description="步序号")
    step_result: StepResult = Field(..., description="本步结果")
    extra: dict[str, Any] = Field(default_factory=dict, description="扩展字段")


# ---------- B8 评估指标（基础 + 扩展） ----------


class RunMetrics(BaseModel):
    """单次 run 的评估指标：基础（成功与否、步骤数）与可选扩展（RPA/鲁棒性、LLM 判分等）。"""
    success: bool = Field(..., description="任务是否成功（与 GT 比对或规则判定）")
    step_count: int = Field(..., description="实际执行步数")
    run_id: str | None = Field(default=None, description="run 唯一标识")
    details: dict[str, Any] = Field(default_factory=dict, description="扩展指标或详情")
    # RPA 与鲁棒性扩展指标（可选，B8 extended 时填充）
    execution_success_rate: float | None = Field(default=None, description="执行成功率：成功 execution 数 / 总 execution 数")
    retry_count: int | None = Field(default=None, description="重试次数（retry_operation 调用数）")
    timeout_count: int | None = Field(default=None, description="超时发生次数")
    timeout_rate: float | None = Field(default=None, description="超时率：超时步数 / 总步数")
    recovery_count: int | None = Field(default=None, description="恢复次数：失败后下一步成功的步数")
    recovery_rate: float | None = Field(default=None, description="恢复率：恢复次数 / 失败步数（无失败则为 1 或 None）")
    llm_judge: dict[str, Any] | None = Field(default=None, description="LLM-as-judge 评分：decision_quality, reasoning_coherence, tool_proficiency, safety_alignment, interpretability 等")
    rule_criteria: dict[str, Any] | None = Field(default=None, description="规则型判据结果：required_tools_passed, required_step_success_passed, details")
    failed_tools: list[str] = Field(default_factory=list, description="失败步骤中出现的工具名（去重排序）")
    error_types: list[str] = Field(default_factory=list, description="失败执行中出现的错误类型（去重排序）")