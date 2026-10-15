# 多 Agent 实现与本地验证

本文描述当前代码中的五角色学习教练。五个角色共享一个应用进程，通过显式流水线按需协作；不要求部署五个服务，也不代表已通过真实模型、搜索端点或 Foundry 部署验收。对外 JSON 协议继续使用 `schema_version: "1.0"`。

## 1. 角色及执行顺序

| 角色 | 工作 | 输出与约束 |
|---|---|---|
| `coordinator` | 识别 A—G 主意图、选择教学方式，优先直接回答，确有必要才请求工具。 | 首次 `decide` 返回内部 `answer` 或 `tools` 模式；对外结果仍为四字段响应。 |
| `researcher` | 把问题拆成 2–4 个可搜索查询。 | 不生成网址或来源；程序执行真正的搜索。 |
| `verifier` | 核查检索材料；以独立调用的 `phase=visual_review` 审阅教学图方案。 | 信源引用必须来自已取得资料；视觉审阅检查声明式内容与教学目标，不能声称已检查真实渲染像素。 |
| `notes` | 从本轮明确表达和受信任的练习证据提取学习笔记。 | 严格 JSON；只有用户明确开启记忆后才运行并写入。 |
| `visualizer` | 按知识点生成带标签、关系和分步变化的图示，必要时按审阅意见重绘。 | 输出声明式 spec；程序绘制 HTML/SVG，不执行模型生成的代码；内容重绘最多一次。 |

```text
请求 → 会话上下文 / 记忆控制命令 → coordinator decide
                                      │
            ┌─────────────────────────┴────────────────────────┐
            │ mode=answer                                      │ mode=tools
            ▼                                                  ▼
    四字段答复 + 内部任务控制                          检查 plan 的工具理由与原文证据
            │                                                  │
      校验并更新状态                                  按需检索 / 当前测验评分与出题
            │                                                  │
            │                                     独立 notes 与 visualizer 并发
            │                                                  │
            │                                     coordinator respond + 工具终态
            │                                                  │
            │                                       最终校验 → 提交笔记 / 产物
            └─────────────────────────┬────────────────────────┘
                                      ▼
                              完整四字段 JSON
```

首次模型调用使用 `phase=decide`。普通讲解、规划、闲聊或必要澄清可在内部 `mode=answer` 中直接给出最终四字段答复及任务控制；校验通过就完成，不再固定执行 `plan → respond` 两次调用。只有所需能力不能由本轮直接回答完成时，才返回 `mode=tools` 的计划，执行选定能力后再调用 `phase=respond`。学习者对当前程序管理的选择题作答必须经过程序评分，不能用直答绕过进度更新。

### 工具选择依据

模型侧没有注册函数工具；下表的“工具”是程序管理的能力。计划中的 `tool_reasons` 为四能力字典：未请求项为 null，请求项含枚举 `code` 和当前学习者消息中的 `evidence_quote`。程序检查理由与请求是否一致、引文是否来自保留首尾空白的当前输入，以及状态和权限条件；原文匹配不证明工具在语义上确有必要，这一项仍须模型行为验收。

引文通常至少含两个非空白字符；只有当前输入去除首尾空白后本身是单字且完整引用时才允许单字，例如 A 或“好”。因此不能把整个 Schema 统一改为 minLength=2。这一检查仅过滤过短引文，不证明工具需求在语义上成立。

| 能力 | 允许的理由 code | 必要条件与执行边界 |
|---|---|---|
| 检索 | `explicit_lookup`、`time_sensitive`、`fact_check`、`knowledge_gap` | 明确查找、时效、核查或知识缺口；researcher 生成查询后由程序检索，取得真实材料后才调用 verifier。 |
| 测验 | `explicit_practice`、`accepted_check`、`continue_practice` | `start` 对应前两项，`continue` 必须对应 `continue_practice`；范围不足先澄清，每次仅一道题，当前选择题答案由程序评分。 |
| 笔记 | `profile_update`、`learning_evidence`、`error_record`、`review_plan` | 必须已开启本会话学习记忆，并且本轮有相应信息或证据；未开启不调用 notes。 |
| 可视化 | `explicit_visual`、`comparison`、`process`、`function`、`active_recall` | 原理、机制和变化可生成通用 scene；也支持 comparison/process/linear_function/flashcards/diagram；外观/场景可用 illustration。需要图不自动意味着需要检索。 |

E/F/G、明确结束或暂停均不启动这些工作能力；分别先处理情绪、安全边界、澄清或停止。直答和工具路径的暂停均将 `next_step` 设为 null。当前等待题收到可明确评分的作答时，除非暂停、结束或停止测验，否则必须走有效的 quiz continue；不能靠改标 E/F/G 或只选其他能力绕过评分。没有必要能力时直接回答或澄清。若实际请求了研究但材料存在争议或未完成核实，不将其转成确定性的教学图。

对此类明确作答，如果 `decide` 已返回结构合法的直答，或工具计划未有效选择 quiz continue，程序从已存待答题重建最小续答计划，不额外调用 `decide`，也不只因选错路由让整轮进入 G。恢复计划使用 C / is_implicit=true，沿用已存 topic、total、difficulty；participant 以已存身份为基础，有本轮工具计划时再保守合并：任一为 companion_report 就保留陪伴者身份，双方均为 learner 才保留 learner，缺失或 unknown 不升级。仅继续评分，学习记忆已开启时可整理本轮可信答题证据；不检索、不生成图。正常有效续答不重建路由，但同样保守合并身份。

无活动待答题、答案有歧义时不强制恢复；暂停、结束和 stop 优先。结构合法的控制计划先停止工作，被忽略的工具理由不阻止控制动作；`mode=tools` 中仅 `quiz_action=stop`、其他能力均未请求也可清理状态，不必为停止虚构需求。测验恢复针对已有待答题的误路由，以及声称练习（intent C）却直答、无法评分且不进状态机的情形；后者重建最小 quiz 计划（无活动测验为 start，主题为答复的任务摘要，已有活动测验为 continue），同样不额外调用 `decide`。工具计划中 continue 而无活动测验时降级为 start，缺少主题则走既有 needs_topic 补问，不整轮兜底。JSON/Schema 错误仍由 runner 做有限格式重试，主角色、出题或汇总调用的真实异常仍按原有兜底处理。

### 完成依据与工具终态

直答的完成依据是最终答复通过结构与业务校验。工具路径只运行一条有界链路：可选 research、notes、visual 能力取得结果或明确失败状态后，交由 `respond` 说明结果和限制，再校验并提交。出题或主流程的致命错误直接进入固定 G 类兜底，不再调用 `respond` 补救。资料不足不触发“把全部角色再跑一遍”的循环。检索与测验阶段之后，独立的 notes、visualizer 并发执行并等待完成；笔记和产物仍在最终校验通过后提交，不脱离本轮后台写入。

`respond.tool_results` 为四能力状态字典；接纳回合的 `state.last_execution` 含 `mode`（answer/tools）和四能力字典 `tools`，保留执行结果。二者都是内部字段，不新增公开响应字段，也不包含选择理由正文。

仅实际恢复路由的回合另记 `state.last_execution.routing_repaired`（待答误路由为 `"pending_quiz_answer"`，声称练习却直答为 `"practice_without_tools"`，明确要求图示却直答为 `"explicit_visual_without_tools"`）；新的正常直答或工具执行记录不写此标记。视觉恢复只识别通用请求，不按知识点选择固定模板；排除拒绝、术语释义、模拟考试和演示文稿，并保留 E/F/G、暂停和结束优先。程序统一计划、quiz、评分证据与 notes 使用的保守合并身份，避免将陪伴者作答或未知来源算作独立掌握；该标记不加入公开四字段 JSON。

| 能力 | 内部状态 |
|---|---|
| 通用 | 未选择为 `not_requested`；被策略禁止为 `skipped_policy`。 |
| research | `verified`、`likely`、`disputed`、`unavailable`。 |
| quiz | `question_ready`、`awaiting_answer`、`feedback_ready`、`finished`、`needs_input`。 |
| notes | 汇总前 `prepared`；新增写入成功后 `saved`；已处理重复或开关关闭等导致未新增写入为 `not_written`；失败为 `failed`。 |
| visual | 汇总前 `ready`，发布成功后 `published`，未完成 `unavailable`。 |

“prepared/ready”只表示可供本轮使用，不能提前声称已保存或发布；最终提交后的状态才写入已接纳的会话结果。`apply_notes` 返回 False 时记录 `not_written` 并说明本轮未新增笔记，不能标为 saved。

## 2. 托管入口与输出协议

应用通过 Agent Framework 的 `BaseAgent` 接入 `ResponsesHostServer`。内部角色消息、搜索结果、校验失败内容和笔记均不直接流给学习者。托管接口仅 yield 已校验完成的最终 JSON；当前不提供逐字流式展示。

托管协议的响应与历史显式使用内存存储，避免 SDK 默认把原始聊天写入用户目录；其生命周期限于当前进程。服务器关闭 SDK 的自动遥测初始化，未接入云端遥测或原始消息采集。每次模型调用结束记录固定字段 `agent_stage`：role、phase（decide/quiz/respond/specialist）、attempt、outcome（ok/invalid_output/provider_or_timeout_failure/cancelled）和 elapsed_ms，不含学习者正文、理由引文或工具材料。该耗时只覆盖本次模型调用，不是整轮总耗时，也不包含搜索网络等待。

所有角色输出按各自 Schema 校验，JSON/Schema 错误在调用预算与超时边界允许时最多重试一次；模型调用异常或超时不走格式重试。致命错误进入原有固定兜底，可选能力失败可局部降级；工具链最终意图必须与计划一致，直答只以自身 `response.intent` 为意图来源。外部 API 原生结构化输出可通过 `LEARNING_COACH_NATIVE_JSON=1` 开启，本地校验始终执行。此开关是否被实际模型及端点完整支持，需要在目标运行环境中验证。

这次减少的是正常直答路径的模型调用和独立能力的串行等待；尚未测量真实端点延迟，不承诺具体秒数。托管层仍整段发送最终合法结果，没有实现 SSE 阶段提示或逐字答案；即时进度提示与更快完成答案是不同能力。

最终响应保持四字段，定义以 [`response.schema.json`](../src/agent-framework-agent-with-local-tools-responses/response.schema.json) 为准：

```json
{
  "schema_version": "1.0",
  "intent": {"primary": "B", "is_implicit": false},
  "message": "先想办法去掉等式左边的常数项，同时保持两边相等。",
  "next_step": {"kind": "question", "text": "等式两边应该同时做什么操作？"}
}
```

`next_step` 可以为 `null`。意图不再通过 `log_intent` 双写，`is_implicit` 只描述主意图的识别方式。来源链接和可视化链接通过可见文本表达，不增加顶层字段。仓库已有 `web/` 本地聊天原型，解析完整响应后读取 `message` 和 `next_step`，本轮不修改该原型；阶段进度、逐字流式及生产化验收未完成。

## 3. 检索与核查

实现文件：[`research.py`](../src/agent-framework-agent-with-local-tools-responses/learning_coach/service/research.py)。接口为 `ResearchService(runner, provider)`，调用 `await service.run(question)` 得到 `queries / sources / claims / overall / warnings`。

默认使用无需密钥的 Wikipedia Action API：先搜索页面，再通过 TextExtracts 获取纯文本节选。设置 `TAVILY_API_KEY` 时选用 Tavily，优先使用其返回的页面原文，原文缺失时明确标为搜索摘要。Tavily 的生成式 `answer` 不进入证据。实际使用 Tavily 可能产生该提供方的用量费用。

每次最多 4 个查询，每个查询最多 3 个结果；单次 API 响应最多 1 MiB，保留每个来源最多 3000 字符。网络请求只发往固定提供方 API 域名，拒绝重定向，不抓取用户或模型给出的任意网址。请求、整组检索等待及返回大小均有边界；网络失败返回 `unavailable`，不编造结果。

程序建立来源 ID、标题、网址、发布方和原始摘录；`excerpt_kind` 区分 `page_extract` 与 `search_snippet`。核查角色只能引用这些来源，程序再验证 `source_id`、逐字摘录和证据关系。

| 标记 | 含义 |
|---|---|
| `verified` | 核查模型判断至少两个独立发布方的页面节选直接支持断言，且通过程序的来源、摘录及独立性检查，没有已引用反证。 |
| `likely` | 有相关支持，但仅有一个发布方、仅搜索摘要，或无法确认独立性。 |
| `disputed` | 提交的有效证据中存在反证，需要向学习者说明局限。 |
| `unavailable` | 没有足够证据、检索失败或核查无法完成。 |

Wikipedia 的不同语言版本视为同一发布方。同域和子域、相同或近似复制的内容不作为独立证据；未知共同所有权和难以识别的转载仍依赖核查模型判断。域名分组是保守的近似方法，并非完整的发布方所有权数据库。`verified` 是本流水线的证据等级，不是对事实正确性的保证；数字、时效和复杂推理仍需真实样本验收。

API 契约依据：[MediaWiki Search](https://www.mediawiki.org/wiki/API:Search)、[TextExtracts](https://www.mediawiki.org/wiki/Extension:TextExtracts#API)、[Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)。

## 4. 画像与记忆

实现文件：[`memory.py`](../src/agent-framework-agent-with-local-tools-responses/learning_coach/db/memory.py)。SQLite 保存结构化 JSON 对应的画像、概念关系、错因、复习事项及练习证据。模型不能直接写入掌握度，分值由程序结合受信任的独立作答证据更新，且属于产品启发式指标。

| 用户命令 | 效果 |
|---|---|
| `/memory on` | 为当前会话明确开启记录和读取。 |
| `/memory off` | 停止记录，并隐藏已有记忆；不等于删除旧数据。 |
| `/forget` | 删除当前会话的已存笔记和同意状态，恢复默认关闭。 |

新会话默认关闭记忆。存储按托管层产生的 `AgentSession.session_id` 隔离，不把模型输出或用户自报的身份当作存储主键。陪伴者表达与学习者自述分别保留来源；没有证据的基础、关系和掌握度保持未知。用户沉默、陪伴者代答、听过解释或说“懂了”不会自动算作独立答对。

当前自动测验累积答题次数和正确次数；受信任的独立作答证据把 `mastery_level` 从中性基线 0 起算（答对按难度 +0.1／+0.15／+0.2，答错 −0.15，同类错因重复再 −0.1），独立复述答对设更高的诊断基线 0.3。该分值是少量观察得出的产品启发式指标，不假装是已校准的掌握度；非独立、陪伴者或未确认的证据不动分。

每轮写入采用事务并按会话和回合去重，失败时不提交半份笔记。数据库可以持久保存，但跨会话身份关联、账号鉴权、多设备同步、数据库迁移与云端备份不在本次实现范围。进程内任务进度也不等于数据库中的学习笔记；重启和多副本需要另外设计会话恢复策略。

## 5. 可视化产物

渲染实现：[`visuals.py`](../src/agent-framework-agent-with-local-tools-responses/learning_coach/adapters/visuals.py)。支持以下声明式 spec：

| `kind` | 内容 |
|---|---|
| `comparison` | 两个对象及逐项对照。 |
| `process` | 顺序步骤与说明。 |
| `flashcards` | 可翻面的知识卡片。 |
| `linear_function` | 固定坐标轴探索一次函数，可显式适配视窗和重置。 |
| `diagram` | 带文字标签的节点关系图。 |
| `illustration` | 外观或场景配图，需要配置可用的生图后端。 |
| `scene` | 根据知识点生成的通用场景与多帧变化，支持逐帧观察和回看。 |

模型仅提供受约束的数据，不提供 HTML、JavaScript 或外部资源 URL。模板转义文案，交互使用固定脚本；互动模板运行时不加载外部资源，illustration 在服务端调用生图后端。预备产物与提交分开，最终回复失败时不提前发布文件。

产物使用随机能力 token，经本地 `/artifacts/{token}` 路由读取，有效期 24 小时。拿到链接的人在有效期内可以访问，它不是登录鉴权链接；内容应限于教学材料。文件及索引位于应用配置的本地存储目录。Foundry 入口或部署网关是否转发此自定义路由尚待部署验收，本地可访问不代表外部链接已可用。

### 通用生成、校验与有界修正

原理、过程和变化由 visualizer 根据当前知识点生成可观察的场景与多帧图解。路由不再用知识点关键词匹配某个固定教学模板；照片、实物外观和场景配图仍可选择 illustration，但不能把一张装饰性配图称为可操作的原理演示。

视觉链路依次执行：`visualizer 生成 → 程序验证 → verifier 视觉方案审阅 → 必要时重绘一次 → 再验证与审阅 → 准备产物 → 最终回复校验 → 发布`。审阅复用现有 verifier，以 `phase=visual_review` 发起独立调用，和信源核查保持不同的输入与协议。未通过时把明确修改意见交回 visualizer；内容重绘最多一次，不循环调用。最终仍未通过或依赖失败时不发布该图，主 Agent 说明未完成与可继续的学习步骤。各阶段 JSON 格式修正仍遵守 runner 的独立次数上限与整轮调用预算。 notes 与视觉链并发准备期间，每次真实模型调用（包括格式重试）都为最终 respond 预留 2 次调用额度；额度不足时可选能力局部降级。视觉生成、审阅及一次重绘共用独立的 60 秒总超时，超时不发布产物，并把失败状态交回主流程继续作答；整轮仍受 180 秒上限约束。

程序验证负责受支持元素、字段类型和数量、坐标范围、同帧标识唯一性、文字容纳与标签重叠及可执行内容隔离；模型审阅负责判断图示是否回应学习目标、关键关系与分步变化是否连贯、是否遗漏必要标签和说明。**模型审阅不等于真实像素验收，也不保证事实正确。** 实际排版、文字可读性与按钮行为仍需要浏览器验证；专业知识和定量结论仍需要可靠来源及独立验收。

scene 是通用交互逐帧图解，不是任意连续物理仿真引擎。模型提供声明式对象及变化，程序用固定版本 SVG.js 3.2.0 绘制；禁止模型提交 HTML、脚本、任意公式执行或外部资源 URL。库、许可证与校验和保存在 `adapters/vendor/`，固定页面资源随源码一起打包；产物嵌入所需脚本并使用内容哈希 CSP，操作时不依赖 CDN 或生图 API。绘图库：[SVG.js 官方文档](https://svgjs.dev/docs/3.2/)。

输入交接：visualizer 收到最近最多 4 条对话（各 1500 字符）、学习目标、资料和画像。输出交接：主 Agent 收到 `visual_context`，包含图类型、标题、内容、实际可操作项、观察问题和适用边界；不会收到图片编码或网页代码；失败时收到 status=unavailable、失败原因与尝试次数，便于准确说明完成状态。Space 保留上一轮面板；图片编码只进入 UI，模型历史保留文字。一次函数拖动时保持坐标轴固定，需要时由学习者显式适配视窗或重置演示。

## 6. 本地运行与验证

应用继续使用项目原有的模型端点和身份配置，新增配置如下：

| 配置 | 默认值 / 用途 |
|---|---|
| `LEARNING_COACH_DATA_DIR` | source 目录下的 `.learning-coach`；保存 `memory.sqlite3` 及 `artifacts` 目录。 |
| `LEARNING_COACH_PUBLIC_BASE_URL` | `http://localhost:8088`；用于生成产物链接，不会自动启动服务或配置公开路由。 |
| `LEARNING_COACH_NATIVE_JSON` | 默认不启用；设为 `1` 请求模型端结构化输出，本地校验始终执行。 |
| `TAVILY_API_KEY` | 可选；配置后使用 Tavily，未配置时使用 Wikipedia。 |

从仓库根启动同一真实流水线的命令行演示：

```bash
uv run --project src/agent-framework-agent-with-local-tools-responses --locked --group dev python src/agent-framework-agent-with-local-tools-responses/demo.py
```

演示需要可用的项目模型端点与凭据。命令行执行本身不等于启动 HTTP 产物服务；打开 `/artifacts/{token}` 链接需要对应的本地托管服务正在运行。

从仓库根运行完整离线测试：

```bash
uv run --project src/agent-framework-agent-with-local-tools-responses --locked --group dev python -m unittest discover -s tests -v
```

测试使用假的角色 runner、假的检索提供方和临时存储，检验实际流水线及错误边界，不发送模型请求或读取真实搜索凭据。通过离线测试说明程序行为符合测试断言，不代表教学内容、线上模型选择、事实核查语义或部署链路已经通过验收。

运行时的实际模型请求、Tavily 请求会按提供方规则计量，不包含在离线测试中。

上线前应分别完成真实模型多轮验收、实际搜索端点失败与限流验收、内存关闭与删除验收，以及公开 URL、持久磁盘和会话恢复验证。当前实现没有创建新云资源，没有部署五个独立服务，也没有执行部署。
