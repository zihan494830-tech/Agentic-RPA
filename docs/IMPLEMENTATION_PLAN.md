# ART 实施计划与状态

> Agentic RPA Testing (ART) Framework for Agent Systems

**Block 定义（B1–B9）、架构、两个闭环、仓库结构**见 [ARCHITECTURE.md](ARCHITECTURE.md)。  
**统一 Block API 格式**见 [API_CONTRACT.md](API_CONTRACT.md)。

---

## 一、当前项目状态

- **框架能力**：B1–B9 契约与实现已贯通；闭环 1（单 run 内 RPA ↔ Agent ↔ 状态）已落地；B8 评估、轨迹落盘、多 run、Poffices 固定/动态场景与 HTTP B1/B8/B9 均可使用。
- **RPA 第三等级（L3，智能 RPA）**：**已基本实现**——在 ScenarioSpec 约束下，goal_driven 与 L3 规划器已接入主线；动态场景可通过 `run_poffices_agent.py` 使用 `--config experiment_poffices_dynamic` 与 `--goal` 做目标导向的流程组合。L3 的进一步细化（如更强的重规划、复合块等）见 [L3_INTELLIGENT_RPA.md](L3_INTELLIGENT_RPA.md)，**属后续优化，择机推进**。
- **闭环 2（跨 run）**：评估结果驱动下一轮出题与 query 策略的部分已可用；「评估结果自动改写 B2/B3/B4 编排参数」等更深闭环**列为后续工作**，不在当前状态叙述中展开。

---

## 二、实施策略：两个闭环

| 闭环 | 策略 | 含义 |
|------|------|------|
| **闭环 1（RPA↔Agent）** | 自始按闭环实现 | 每步：RPA 结果写入状态 → 下一轮 Agent 输入携带最近 ExecutionResult；先 mock 跑通再换真实 RPA。 |
| **闭环 2（Evaluation↔Orchestrator）** | 先测通评估与多 run，再加深耦合 | B8、多 run、对比与落盘先独立可用；历史 metrics 驱动下一批实验编排参数可作为后续扩展。 |

---

## 三、依赖关系与模块顺序

```
B1 ─────────────────────────────────────────────────────────────┐
     │                                                            │
     ▼                                                            ▼
B2 ──► B3 ──► B4 ──► B9 (Orchestrator) ──► 需要 B5,B6,B7 实现   B8
     │         │         │                      │    │    │       ▲
     │         │         │                      ▼    ▼    ▼       │
     │         │         └─────────────────► B5 ◄── B6   B7 ──────┘
     │         │     闭环 1：RPA↔Agent
     │         └────────── 闭环 2：Evaluation↔Orchestrator ────────┘
```

- **骨架与闭环 1**：B1、B5、B7（含 ExecutionResult）、B6、B9 串联；mock 与真实 RPA、最小 B8 评估。
- **编排层**：B2、B3、B4；B6 多角色/多决策组件插槽。
- **评估与鲁棒性**：B8 扩展指标、故障注入包装、多 run 聚合与对比脚本。
- **平台化与闭环 2 加深**：实验矩阵、历史 metrics 驱动编排——**后续优化**。

---

## 四、各 Block 单测与集成测试（摘要）

| Block | 单测 | 集成测试 |
|-------|------|----------|
| B1 | 解析 2 种 config、非法 json/yaml | 与 B9 联调：加载后能跑 1 步 |
| B2 | 边界难度、路由分支 | B2+B3：某 route_type 得到预期 DAG |
| B3 | DAG 构建、next_steps 拓扑 | B3+B9：线性/分支各 1 条轨迹 |
| B4 | 路由表、Agent 分配 | B4+B6 mock：步骤→正确 Agent 与工具 |
| B5 | 状态更新、轨迹 append/序列化 | B5+B9：多步后状态与轨迹一致 |
| B6 | mock LLM 固定输出→解析 tool_calls | B6+B7 mock：一次闭环决策 |
| B7 | mock UI/错误→ExecutionResult 格式 | B7+真实 RPA：返回闭环所需字段 |
| B8 | 固定轨迹→固定分数 | B8+B5：从真实轨迹产出指标 |
| B9 | mock B2–B7：完整 1 task | 端到端 1 场景 1 编排 |

Block 职责与代码位置见 [ARCHITECTURE.md](ARCHITECTURE.md)。

**指标与轨迹**：B8 含 success、step_count、run_id 及扩展指标（execution_success_rate、retry、timeout、recovery、可选 llm_judge 等）；`rpa_mode`：normal / robustness / stress。轨迹落盘含 `schema_version` 与 `extra.run_record` 等；重放：`scripts/replay_trajectory.py`；导出：`scripts/export_trajectory.py`。多场景对比：`scripts/run_phase3_matrix.py`、`scripts/aggregate_phase3_runs.py`（脚本名保留历史，功能为通用聚合对比）。

---

## 五、总结与后续方向

- **当前**：ART 作为测试框架主体，对「待测 Agent」做多维度测试；**RPA L3（智能 RPA）主线已基本实现**；Poffices 统一入口 `run_poffices_agent.py`（含 `--runs`、`experiment_poffices_dynamic`、`--goal`）。
- **Block 化**：B1–B9 接口清晰、可 mock、可单独测试。
- **后续（不展开排期）**：闭环 2 与编排参数联动、实验流水线、可复现性与 L3 内部增强等，按需迭代。

L3 设计细节与演进思路见 [L3_INTELLIGENT_RPA.md](L3_INTELLIGENT_RPA.md)。
