> 历史核查记录：当前多 Agent 已接入本地输出校验和有限重试；可选原生 JSON 模式见 [当前实现](multi-agent.md)。原占位 generate_quiz 工具已被程序管理的逐题流程替代，下文描述的是此前阶段。

# 输出协议验收与运行时支持核查

核查日期：2026-10-08。精简后的 `instructions.md` 不变，本次增加程序验收能力。

## 当前已实现与尚未接入

`scripts/validate_responses.py` 对保存的最终响应逐条执行严格 JSON 解析及 `response.schema.json` 校验，失败也继续处理，并输出失败索引。它可处理实际模型响应，但本次验证使用的是合成样本；尚无真实模型通过率。

`main.py` 仍只配置 `default_options={"store": False}`，没有启用结构化输出，也没有生产校验/重试包装。因此当前服务仍可能返回不合规文本。验收脚本不会自动截获服务响应，需要由采集流程保存并提交每轮完整输出。

## 如何使用

从仓库根，使用项目 `dev` 依赖组执行；该组显式固定 `jsonschema==4.26.0`，并由 `uv.lock` 锁定：

```bash
uv run --project src/agent-framework-agent-with-local-tools-responses --locked --group dev python scripts/validate_responses.py final-response.txt --format raw-json --failures failures.jsonl
```

| 输入格式 | `--format` | 约定 |
|---|---|---|
| 一轮完整 assistant 最终文本 | `raw-json` | 文件整体是一份候选响应，允许多行 |
| 每行一份最终文本 | `raw-jsonl` | 单行 JSON 不跨物理行，空行跳过 |
| 每行一个 Responses API 响应对象 | `responses-jsonl` | 顶层须 completed；唯一 assistant 消息、唯一 output_text |

Responses 模式不检查工具/推理内容，不拼接有歧义的多消息或多文本块。拒绝块、未完成输出和无法提取最终文本均算失败。CLI 日志和 SSE 事件不属于上述输入格式；采集端须先按真实传输协议提取或组装，不能假定 `azd ai agent invoke` 的完整控制台输出就是最终文本。

脚本拒绝重复键、NaN/Infinity、围栏、前后附加说明及 Schema 不合规内容，不自动修复。输出摘要包含总数、通过数和失败数；退出码 `0` 为非空样本全过，`1` 为样本失败，`2` 为配置/文件错误或没有样本。

`--failures` 必填，写入 JSONL。报告只保存样本序号、原文件行号、候选原始字节的 SHA-256，以及错误阶段、错误代码和 Schema 路径。保留原始输入文件，并在验收记录中将其关联到用例和回合，才能定位原失败样本。报告默认禁止覆盖；显式 `--overwrite` 可替换旧报告，但不能覆盖输入或 Schema。

## Agent Framework 支持情况

[Microsoft 官方结构化输出文档](https://learn.microsoft.com/en-us/agent-framework/agents/structured-outputs) 说明：Python Agent 可在 `options` 或 `default_options` 中设置 `response_format`，接受 Pydantic 模型或 JSON Schema 字典，具体能力取决于客户端及模型。

对仓库 `uv.lock` 锁定版本的公开发行包做了静态源码核查，下载包的 SHA-256 与锁文件一致：

| 包 | 锁定版本 | 相关路径与结论 |
|---|---|---|
| agent-framework-core | 1.17.0 | `_agents.py` 将 response_format 传入选项；`_types.py` 对字典格式的响应值调用 json.loads，并不等于完整 JSON Schema 校验 |
| agent-framework-foundry | 1.12.0 | `_chat_client.py` 的 FoundryChatClient 使用 Responses API 客户端实现 |
| agent-framework-openai | 1.14.2 | `_chat_client.py` 将字典形式 response_format 转为 Responses 的 text.format/json_schema 配置 |

这证明锁定 SDK 有接入能力，不证明实际部署的模型、API 版本和完整 Schema 都兼容。待容器环境验证后，可在 `main.py` 读取同目录 Schema，并在现有 `default_options` 中加入 `response_format`。该配置方案本次没有启用。

容器阶段须确认实际安装版本、模型端点接受的 Schema，以及正常输出、截断、拒绝和流式结束行为。即使启用原生结构化输出，也保留本地 Schema 校验。若端点不支持，则实现有次数上限的校验—重试和失败兜底；工具副作用、重复日志与测验进度须避免因重试而重复执行。不能以静默退回普通文本替代失败处理。

## count 守卫为何保留

在上述锁定版本中，普通 `FunctionTool.invoke(arguments=...)` 先调用 Pydantic 输入模型校验，`count=2` 会在进入函数体前失败。这条路径下，函数体的 `ValueError` 确实不会负责拦截它。

但 `FunctionTool.__call__` 直接调用原函数：Python 代码直接执行 `generate_quiz(..., count=2)` 会进入函数体并触发守卫。因此它不是全局不可达代码。合法 `count=1` 时，`if type(count) is not int or count != 1` 的条件是**假**，不会抛异常。

保留守卫并补充注释。`tests/test_framework_tool_paths.py` 分别覆盖这两条 SDK 路径；SDK 不可用时明确跳过，不把隔离函数测试当作 SDK 集成证明。

## 本次验证边界

首次验收脚本验证记录：离线测试共 31 项：29 项通过、2 项因本机缺少 Foundry SDK 而跳过。包含已有契约测试和新增验收脚本测试；不包含模型请求、部署或实际容器兼容性验证。Schema 合规也不证明意图判断或教学内容正确，行为验收仍按 `docs/acceptance-v1.5.md` 执行。

开发依赖复验：使用隔离环境按更新后的锁文件只安装 `dev` 组，确认 `jsonschema` 为 4.26.0；31 项中 29 项通过，2 项 SDK 测试因该环境未安装运行依赖而跳过。锁文件中的所有原有包版本保持不变。
