# Edu-Agent 学习教练

一个面向教育场景的多 agent 对话系统，运行在 **Azure AI Foundry** 上，基于 Microsoft Agent Framework 构建。

它要解决的问题不是"让 LLM 回答问题"——这件事直接调模型就能做——而是：**当模型输出本质上是概率性的、不可信的时候，一个教育产品如何被交付给用户。** 本项目给出的答案是：协议校验、能力分级、显式失败和可观测的意图决策。

## 架构

五个真实运行的 in-process agent，由主 agent 做任务编排：

```text
learner → coordinator (decide) ── 直答 → 校验 → 最终四字段 JSON
                    │
                    └─ 需要工具 → 检索/查证（researcher + verifier）
                    │             出题判分（quiz 状态机）
                    │             笔记 + 可视化（并行、独立）
                    ↓
            coordinator (respond) → 运行时校验 → 发布 → 最终四字段 JSON
```

- **coordinator**：意图识别、任务编排、面向学习者的回复
- **researcher**：资料检索（默认 Wikipedia Action API，可选 Tavily）
- **verifier**：信源交叉查证，输出证据分级
- **notes**：学习笔记整理（错题归因、难点归纳）
- **visualizer**：声明式互动图生成（模型不可提交可执行代码）

## 核心设计决策

这些是本仓库区别于"一个带工具的聊天 bot"的部分：

1. **校验过的 wire protocol**。对外只有四字段协议（`schema_version / intent / message / next_step`，见 `response.schema.json`）。每个角色的模型输出都过本地 schema 校验，格式失败在调用预算内最多重试一次，仍失败则走固定兜底文案；笔记或可视化失败允许局部降级并在回复中明示（"这次未生成可用的互动图"）。
2. **意图分类体系**。七类学习意图（讲解/举例/练习/测验/笔记/可视化/边界与情绪），区分显式与隐式意图（`is_implicit`），复合意图由对话状态机承接。意图依据与归因分类的来源讨论见 `docs/multi-agent.md`。每轮只记录最终生效的意图，供离线评测。
3. **信源查证而非"听起来对"**。引用必须来自实际检索到的记录；证据分级（verified / likely / disputed / unavailable）直接决定回复措辞和能否出图；资料不足时回复明确标注，不装作确定。
4. **opt-in 学习记忆**。SQLite 存储学习笔记（带来源引用），`/memory on` 开启、`/memory off` 暂停、`/forget` 删除本会话全部记录。未校准的掌握度保持"未知"，观察记录不等于能力验证。
5. **服务端出题状态机**。一次只出一道题，答案 key 是私有程序状态、永远不进模型上下文；未作答时不强推下一题；会话内支持暂停/继续/结束任务。
6. **声明式可视化**。对比、流程、记忆卡、线性函数四种模板渲染为应用自持的 HTML/JS；模型只能填参数，不能写代码，互动图以 artifact 链接交付（本地可打开，云端网关暂不透传该路由，见"已知限制"）。
7. **可观测性**。每次模型调用记录 `agent_stage`（角色/阶段/尝试/耗时，不含消息内容）；意图日志按轮哈希脱敏。评测用例在 `evals/cases.jsonl`。

## 快速开始

前置：Python 3.13+、[uv](https://docs.astral.sh/uv/)、Azure 凭据，以及按 `src/agent-framework-agent-with-local-tools-responses/.env.example` 配置的 `.env`。

本地跑 agent（Responses 协议服务，端口 8088，含互动图路由）：

```bash
uv run --project src/agent-framework-agent-with-local-tools-responses --locked --group dev \
  python src/agent-framework-agent-with-local-tools-responses/main.py
```

本地聊天页（端口 8600，无状态转发 + 对话 UI）：

```bash
python3 web/serve.py   # 打开 http://localhost:8600
```

交互式脚本（不走服务）：

```bash
uv run --project src/agent-framework-agent-with-local-tools-responses --locked --group dev \
  python src/agent-framework-agent-with-local-tools-responses/demo.py
```

部署到 Foundry：`azd deploy`（详见下文 azd 一节）。本地状态默认写在源码目录 `.learning-coach/`，可用 `LEARNING_COACH_DATA_DIR` 改位置。

## 测试与验收

```bash
uv run --project src/agent-framework-agent-with-local-tools-responses --locked --group dev \
  python -m unittest discover -s tests -v
```

测试套件用 fake 模型/检索响应覆盖真实 SDK 接口：直答单调用路径、选择性工具路由、存储、证据校验、测验回放、渲染。不调用真实模型，不评估教学准确性。当前 165 个用例全绿。

真实响应的离线校验：

```bash
uv run --project src/agent-framework-agent-with-local-tools-responses --locked --group dev \
  python scripts/validate_responses.py final-responses.jsonl --format raw-jsonl --failures failures.jsonl
```

## 已知限制（诚实清单）

- **无流式输出**：一轮一个完整文本。
- **单进程本地实现**：无账号体系、无跨会话学习者身份、无分布式会话恢复。
- **云端互动图打不开**：artifact 链接指向 `localhost:8088`，Foundry 网关不透传自定义路由（实测多种路径 404）；后续需将 ArtifactStore 换成 blob 存储。
- **评测进行中**：意图分类与隐式意图识别的准确率尚未量化（`is_implicit` 误标问题待评测集验证）。
- 出题的教学质量、长期学习效果未经真实用户验证。

## 文档导航

| 文件 | 内容 |
| --- | --- |
| `docs/multi-agent.md` | 多 agent 实现细节与设计取舍 |
| `docs/edu-agent-architecture.md` | 架构说明 |
| `docs/acceptance-v1.5.md` | 行为验收标准 |
| `docs/frontend-handoff.md` | 前端对接说明（协议、运行模式、已知边界） |
| `evals/cases.jsonl` | 评测用例 |

## 部署（Azure Developer CLI）

前置：`azd` + `azd ext install microsoft.foundry` + `azd auth login`。

```bash
azd ai agent run                          # 本地运行（8088）
azd ai agent invoke --local "请围绕一元一次方程考考我，一次只出一道题。"
azd deploy                                # 部署到 Foundry
azd ai agent invoke "请围绕一元一次方程考考我，一次只出一道题。"
```

也可以用 VS Code Foundry Toolkit 扩展调试与部署（F5 启动 + Agent Inspector）。完整指南见 [Deploy a hosted agent](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent)。

## Roadmap

- 意图识别与归因的量化评测（评测集已建，跑分与阈值待定）
- 互动图产物迁移到 blob 存储，云端可打开
- 流式输出与生产级前端
- 真实用户试用与迭代记录
