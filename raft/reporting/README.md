# raft.reporting — 测试报告模块

接入 LLM 完成「整理并输出」多轮测试报告，与原有报告逻辑一致，并明确与 LLM-as-judge 的包含关系。

## 模块逻辑（是否沿用之前报告要求）

**是，完全沿用。** 流程如下：

1. **构建各轮摘要**  
   从多轮 `results` 生成 `rounds_summaries`，每轮包含：  
   `run_id`、`success`、`step_count`、`details`、**`llm_judge`**（B8 单轮评分）、**`output_snippet`**（约 1200 字输出原文）。

2. **调用 LLM 生成多轮总结**  
   调用 `raft.core.llm_judge.summarize_multi_rounds`，生成「LLM 多轮分析总结」文案，要求：  
   有据可依、引用输出原文、遵循评估基准时间/地点（不因 2025 年数据或 HKD/香港扣分）。

3. **生成 HTML 报告**  
   调用与之前相同的 `build_multi_flow_report`，产出完整报告，包括：  
   实验配置与待测场景、各 Block 运作、多轮汇总、多轮明细（每轮完整输入/输出、本轮 LLM 简要分析、本 run 各 Block 运作）、LLM 多轮分析总结。  
   排版、格式、分段与原有报告一致。

## 与 LLM-as-judge 的关系：包含关系，不是两个 LLM

- **LLM 能力**统一在 **`raft.core.llm_judge`**，使用同一套 API 配置（如 `OPENAI_API_KEY`、`RAFT_LLM_PROVIDER`（ART 框架 LLM 提供商环境变量，名称保持不变以兼容现有配置））。

- **两处调用、同一模块**：

  | 调用 | 时机 | 作用 |
  |------|------|------|
  | **judge_trajectory** | B8/Orchestrator 落盘时，**每轮一次** | 对单轮轨迹打分（decision_quality、reasoning_coherence、tool_proficiency、output_quality、safety_alignment、interpretability、output_comment），结果写入该轮 `metrics.llm_judge`。 |
  | **summarize_multi_rounds** | **本报告模块**内，**全部跑完后一次** | 根据各轮摘要（含各轮 `llm_judge` 与输出原文）生成整份「LLM 多轮分析总结」段落。 |

- **报告模块**没有引入第二个 LLM：  
  - **复用**单轮 LLM-as-judge 的产出：每轮 `metrics.llm_judge` 用于报告中的「本轮 LLM 简要分析」。  
  - **调用**多轮总结：`summarize_multi_rounds` 生成报告最后一节的总结文案。  
  - 再交给同一套 HTML 模板输出。

因此是 **「报告模块包含/使用 LLM-as-judge 模块」**，不是两个独立的 LLM 系统。

## 待测 Agent 输出范围（首尾系统格式不计入）

本模块规定：**每一轮开头和结尾的「系统格式」不计入待测 Agent 的输出范围内**。

- **系统格式示例**：Market Analyzer 标题、问候语（Good morning/afternoon/evening, ...!）、「Team briefing on...」、结尾「The team is mobilized and ready to execute.」「I'll keep you informed...」等。
- **仅将去除上述首尾后的内容**视为待测 Agent 输出，用于：
  - 各轮摘要中的 `output_snippet`（供 LLM 多轮总结引用）
  - 报告中的「本轮输出（待测 Agent 输出，已去除首尾系统格式）」
  - 单轮 LLM-as-judge 判分时的「待测 Agent 的输出内容」

实现见 `raft.reporting.output_scope.strip_system_format_from_agent_output`；报告 HTML 与 `llm_judge` 判分均调用该函数，保证口径一致。

可选 **LLM 正文提取**：设置环境变量 `RAFT_LLM_EXTRACT_BODY=1` 或 `true` 后，正文范围将优先由 LLM 从原始输出中抽取（去掉问候、任务说明、Time of preparation/completion、Disclaimer 等），失败或未配置时自动回退到上述规则逻辑。API 与 llm_judge 共用（`RAFT_LLM_PROVIDER`、`OPENAI_API_KEY` / `XAI_API_KEY`）；可选 `RAFT_LLM_EXTRACT_MODEL` 指定模型，否则沿用 judge 或默认 `gpt-4o-mini`。实现见 `raft.reporting.llm_extract`。

## 报告生成耗时

- **轨迹日志目录** 打印后，报告生成分为两步：**(1) 调用 LLM 生成多轮分析总结**（`summarize_multi_rounds`）、**(2) 构建 HTML 并写入文件**。
- **耗时主要来自 (1)**：与所选 LLM API 有关（如 OpenAI / Qwen / 其他），通常 **1–3 分钟** 属正常范围；模型越大、输入越长或网络较慢时可能更长。若未配置 API 或调用失败，会跳过总结、仅生成明细报告，耗时仅数秒。
- **默认（mini 模式）**：不调 LLM 总结，生成最简报告（无判分、无总结），仅输入/输出/轨迹/Block 步骤，报告几乎立即完成。
- **需要完整报告时**：运行入口加 `--full-report`，启用 LLM 判分与多轮总结。
