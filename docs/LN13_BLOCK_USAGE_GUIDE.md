# LN13 Block Usage Guide

这份文档基于 `LN13-GenAI Agents Development Platform.pdf` 以及当前项目中的画布示例整理，目标是给后续所有 `import JSON` 修改提供统一约束。以后新增、修改、排查 LN13 画布时，优先遵守本文，而不是凭经验猜测节点行为。

## 1. 总原则

- LN13 画布中的大多数 block 都依赖“运行时变量替换”。
- 变量写法统一使用 `{variable_name}`。
- 跨节点引用优先使用：
  - `{layer_output_1}`
  - `{layer_name_xxx_output}`
  - `{layer_name_xxx_output.field}`
- 如果某个 `{token}` 在运行记录里没有被替换，优先检查：
  - block `name` 是否拼写正确
  - JSON 字段路径是否存在
  - 当前节点是否真的能访问该变量

## 2. 变量系统

### 2.1 基础变量

常用系统变量包括：

- `{query}`
- `{language}`
- `{agent_name}`
- `{agent_id}`
- `{date_utc}`
- `{datetime}`
- `{user_name}`
- `{user_email}`

### 2.2 图层输出变量

LN13 支持直接引用前序层输出：

- `{layer_output_1}`：按执行顺序引用第 1 个 block 输出
- `{layer_name_searcher_output}`：按 `name` 引用某个 block 输出
- `{layer_name_searcher_output.title}`：如果输出是 JSON，可直接取字段

### 2.3 JSON 智能解析

如果某层输出是：

```json
{"title":"Hello","score":95}
```

则：

- `{layer_name_xxx_output}` 返回整体输出
- `{layer_name_xxx_output.title}` 返回 `Hello`
- `{layer_name_xxx_output.score}` 返回 `95`

## 3. Block 分类与使用方法

LN13 的 block 可以大致分为 4 类：

- 输入/分析类
- 请求/API 类
- 生成/整合类
- AoA/父子代理类

后续改 JSON 时必须先判断当前 block 属于哪一类，再决定 `prompt` 应该写自然语言还是 JSON Request Body。

## 4. 各类 Block 用法

### 4.1 Input Analysis

用途：

- 工作流起点
- 理解用户输入
- 输出结构化 JSON，供下游 block 使用

推荐写法：

- 输出固定 JSON 结构
- 保证 key 稳定
- 后续节点统一从 `{layer_name_input-analysis_output.xxx}` 取值

适合输出：

- `ultimate_goal`
- `keywords`
- `language`
- `constraints`
- `short_summary_for_user`

约束：

- 不要在这里做真正检索
- 不要替代 planner 决策
- 最好只做“理解 + 结构化”

### 4.2 Web Search

用途：

- 做实时网页搜索
- 适合拿摘要、链接、图片引用

特点：

- 文档注明这是旧版路径，在 AoA 版本里不一定是首选
- 支持迭代次数、区域、语言、返回类型

可用输出变量示例：

- `{web_evaluation(1)}`
- `{web_links(1)}`
- `{image_links(1)}`
- `{search_web_content}`
- `{search_web_link}`
- `{search_images}`

### 4.3 Web Crawler

用途：

- 深度爬取网页内容、摘要或图片

适合：

- 已经有 query 或 URL，需要进一步拉正文

常见配置点：

- 搜索结果数量
- 去重
- 日期范围
- 相关度 / 日期排序

### 4.4 Web Scraper

用途：

- 抓取网页完整内容

关键规则：

- 这是 `Request` 型节点，不是普通自然语言 LLM prompt 节点
- `prompt` 必须优先理解为“请求体”，不是“写给模型的一段说明”

后续修改要求：

- 先去 API 文档或现有成功案例确认字段名
- 不要随意把它改成：
  - `You are wired to...`
  - `Please summarize...`
- 这类自然语言写法容易被平台当成 JSON 请求体解析并报错

实践建议：

- 如果接口只需要 `query`，就只传 `query`
- 不要传空字符串 `url`
- 不要同时传一堆未确认的字段名，例如 `query_text`、`instruction`、`content`，除非已验证接口支持

### 4.5 DB Search / File Search / Knowledge DB

这类节点都属于“Request Body + API endpoint”风格：

- DB Search：查上传文档/结构化数据
- File Search：查文件系统或资产
- Knowledge DB：查已预抓取知识库

共同规则：

- `prompt`/请求区应写结构化参数
- 字段名要与后端实际 API 一致
- 不要混入自然语言说明作为请求体

### 4.6 Ranking

用途：

- 对 crawler/search 结果排序和过滤

适合：

- 搜索结果很多，需要筛选优先级

### 4.7 Data Integration / Merge

两者都用于汇总数据，但语义不同：

- `Data Integration`：偏“结构化合并”，强调字段映射、融合策略
- `Merge`：偏“模板替换”，不依赖 LLM，直接把多个输入拼接成输出

`Merge` 的关键规则：

- 更适合做稳定 JSON 拼装
- 如果只是把多个前序结果封装成一个 JSON 给下游，优先选 `Merge`
- 不要在 `Merge` 里做复杂推理

### 4.8 LLM / Content

用途：

- 内容生成
- 中间处理
- 最终回复

特点：

- 这是自然语言 prompt 最适合使用的节点
- 如果你想“解释 planner 输出”“抽取字段”“生成最终答复”，优先使用这个节点

适用场景：

- 解析 planner 返回 JSON
- 抽取 `selected_agent`
- 生成最终面向用户的回答

不适用场景：

- 直接替代 API 请求型 block 的 Request Body

### 4.9 Custom Block

文档中强调自定义逻辑块分两类：

- `LLM mode`：`llm != null`
- `Non-LLM mode`：`llm == null`，做 JSON 输入输出

使用规范：

- 如果这个 block 的本质是“调用某个已有 API / planner / FastAPI 服务”，优先按 API 契约写 JSON
- 如果这个 block 的本质是“用模型做解析或生成”，才用自然语言 prompt

文档推荐做法：

- 打开对应 FastAPI `/docs`
- 用 `Try it out`
- 复制 example value 作为输入参数模板

这条规则以后必须遵守，尤其是接入 `web-scraper`、`planner`、`自建 API` 时。

## 5. AoA 专项规范

这是后续改 JSON 最容易出错的部分。

### 5.1 Agent Over Agent 的角色

`Agent Over Agent` 是父代理到子代理的桥接层：

- 父 Agent -> `Agent Over Agent` -> 子 Agent

它的作用不是直接完成业务，而是把结构化数据传给子代理。

### 5.2 AoA Input 的角色

`AoA Input` 是子代理入口。

LN13 文档里的核心规则是：

- 父 AoA 层传入一个 JSON
- `AoA Input` 使用同一组 key 接收
- 这些 key 会变成子 Agent 内可用变量

文档给出的语义是：

- `{Key: "VALUE"} => {Key} = "VALUE"`

也就是说：

- 父层传 `{"query":"abc"}`
- 子层就应该直接能用 `{query}`

### 5.3 AoA Output 的角色

`AoA Output` 是子代理出口。

职责：

- 把子代理最终结果重新包装成 JSON 返回父层

最稳的用法：

- 只返回必要字段
- 保持 key 稳定
- 不要在这里再做复杂逻辑

例如：

```json
{
  "web_scraper_result": "{layer_name_web-scraper-step_output}"
}
```

### 5.4 AoA 修改规则

以后改 AoA 相关 JSON，必须遵守：

1. 父 `agentNode` 的 Request Body 和子 `nestedInputNode` 的 JSON key 保持一致
2. 子 `nestedInputNode` 不要凭空改名，比如把 `query` 改成 `query_text`，除非整条链路一起改
3. 子代理里实际使用的变量名，必须来自 `AoA Input` 传入的 key
4. 子 `nestedOutputNode` 返回字段要稳定，父层下游只读取这些字段
5. AoA 先跑通最小字段集，再逐步加字段

### 5.5 AoA 推荐最小闭环

如果只是验证“父层规划 -> 子层执行 -> 回传结果”，建议最小化：

父层 `agent-over-agent`：

```json
{
  "query": "{layer_name_xxx_output.query}"
}
```

子层 `AoA Input`：

```json
{
  "query": "{query}"
}
```

子层执行节点：

- 直接用 `{query}`

子层 `AoA Output`：

```json
{
  "result": "{layer_name_xxx_output}"
}
```

### 5.6 Poffices AoA 使用两步流程（发布子 Agent + 启用父 AOA）
你在 `poffices` 的实际使用是“两步走”，建议写入配置/排障时的标准动作：
1. 先将“要被父 Agent 调用的子 Agent”发布到 `poffices`（确保父层可在 AOA 选择器里找到它，通常需要拿到 `agent_id` 或可选的唯一标识）。
2. 在 `poffices` 里给该“子 Agent”开启 `AOA 模式`（使其具备 `AoA Input / AoA Output` 的子链路入口能力）。
3. 打开父 Agent 里的 `AoA block`，在 `AoA block` 的“子 Agent/Agent（nested agent）”选择器中，选择第 1 步刚发布并已启用 AOA 模式的子 Agent。
4. 完成上述选择后再运行：父层会把 `AoA Input` 需要的 JSON key 传入子代理；子层按 `AoA Output` 返回结构化结果，父层再把结果接到下游链路。

注意事项（与 5.4/5.5 强相关）：
- `agent-over-agent`（父）Request Body 的 key 必须与子 `AoA Input` 的 JSON key 对齐（避免变量漂移）。
- 子 `AoA Output` 的返回字段要稳定，父层下游只读取约定字段。

## 6. 当前项目中已验证的做法

### 6.1 Planner 节点

在本项目中，`customNode_117` 已被用于调用 planner。

推荐模式：

- `Input Analysis` 先产出结构化目标
- `customNode_117` 调 planner
- 用一个普通 `LLM/Content` 节点解析 planner 返回 JSON
- 再把解析结果喂给下游

不推荐：

- 让 AoA 直接消费 planner 原始大 JSON

### 6.2 Route2 示例的启发

当前项目内 `docs/poffices_canvas_flow_route2_no_merge.json` 的结构说明了一种更稳的做法：

- 先 planner
- 再用独立 LLM 节点抽取 `selected_agent`
- `agent-over-agent` 只接收简单字段：
  - `agent`
  - `content`

这说明：

- “planner 原始输出解析”和“AoA 执行”最好拆开
- 不要把两种职责塞进同一个 block

## 7. 调试与排障流程

LN13 文档明确推荐使用 Generation Record 排查：

- 每个 block 的输入/输出
- 变量替换结果
- LLM 调用与响应
- 分支判断
- 循环迭代
- 错误和跳过信息

以后排障按这个顺序检查：

1. 看 `Input` 是否已有正确值
2. 看 `{token}` 是否被替换
3. 看 block 输出是否是期望格式
4. 看下游 block 是不是按正确字段名在取值
5. 如果是 API block，去 `/docs` 核对请求体

## 8. 后续修改 import JSON 的强制规则

以后任何人修改 LN13 画布 JSON，都必须遵守下面这些规则：

### 8.1 字段与变量

- 不要随意改 block `name`
- 改了 `name` 必须同步修改所有 `{layer_name_xxx_output}`
- 不要引入未验证的字段名
- 同一条链路中字段名必须前后一致

### 8.2 Request 型节点

以下节点优先按“请求体”理解，不按“自然语言 prompt”理解：

- Web Scraper
- DB Search
- File Search
- Knowledge DB
- 部分 Custom API Block
- AoA Input / AoA Output

规则：

- 先查接口文档
- 先用最小字段集
- 不传空字符串可选字段，除非接口明确允许

### 8.3 LLM 型节点

以下节点适合写自然语言 prompt：

- Input Analysis
- LLM / Content
- 解析 planner 输出的中间节点
- 最终回复节点

### 8.4 AoA

- 父 AoA Request Body 与子 AoA Input key 保持一致
- 子 AoA Output 只回传稳定字段
- 先用单字段最小链路验证
- 跑通后再扩展 `language`、`url`、`content` 等附加字段

## 9. 本项目当前经验结论

结合最近这次排障，已经可以明确：

- `AoA Input` / `AoA Output` 必须显式配置 Request Body
- `web-scraper` 不能再写成普通自然语言说明 prompt
- planner 输出最好先经过单独的中间解析节点
- 如果子链路有 `Http Exception`，优先怀疑：
  - 请求字段名不匹配
  - 传了空字段
  - 把 Request 型节点误写成 LLM prompt

## 10. 推荐工作流模板

后续若做“planner -> web-scraper -> final summary”类流程，建议按下面顺序设计：

1. `Input Analysis`
2. `Planner (custom block)`
3. `LLM parser`：从 planner 结果抽取最小字段集
4. `Agent Over Agent`
5. `AoA Input`
6. `Request 型执行节点`
7. `AoA Output`
8. `Final LLM`

其中第 3 步输出建议尽量小，例如：

```json
{
  "query": "..."
}
```

不要一开始就传：

```json
{
  "agent": "...",
  "content": "...",
  "query_text": "...",
  "instruction": "...",
  "url": "",
  "language": "..."
}
```

字段越多，变量漂移和契约不一致的风险越大。

## 11. 使用本文档的方式

以后每次修改画布 JSON 前，先回答这 4 个问题：

1. 当前节点是 LLM 型还是 Request 型？
2. 这个字段名在上下游是否完全一致？
3. 这个变量会不会在运行时被真正替换？
4. 这个请求体是不是来自已验证的接口契约？

只要其中有一个答不上来，就先不要改 JSON，先查文档或成功案例。
 