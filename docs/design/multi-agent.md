# 多 Agent 实现与本地验证

本文描述当前代码里的一个主 agent 和四个子 agent 怎样协作。五个 agent 共享一个应用进程，由程序按显式流水线调度；不需要部署五个服务，也不代表已经通过真实模型、搜索端点或 Foundry 部署验收。对外 JSON 协议继续使用 `schema_version: "1.0"`。代码分层、模型分工和数据存放见 [`architecture.md`](architecture.md)。

## 1. 角色及执行顺序

| 角色 | 提示词 | 工作 | 输出与约束 |
|---|---|---|---|
| `coordinator` 主 agent | `prompts/instructions.md` | 识别 A–G 主意图、选择教学方式，优先直接回答，确有必要才派活；需要查资料时写检索问题和 2–4 条搜索词；汇总结果写最终回复；讲完一块时可以要一道检查题。 | 首次 `decide` 返回内部 `answer` 或 `tools` 模式；同时返回更新后的对话要点（`context_notes`）。对外结果仍为四字段响应。 |
| `verifier` 核对 | `source-check.md` | 核查检索材料；以独立调用的 `phase=visual_review` 审阅教学图方案。 | 信源引用必须来自已取得的资料；审图检查声明式内容与教学目标，不能声称检查过真实渲染的像素。 |
| `visualizer` 画图 | `visual-designer.md` | 按知识点设计带标签、关系和分步变化的图示，必要时按审阅意见重画。 | 输出声明式 spec；程序绘制 HTML/SVG，不执行模型写的代码；内容重画最多一次。 |
| `quizzer` 出题 | `quiz-writer.md` | 每次写一道单选题，给每个错误选项标疑似错因；需要时请求一张配图。 | 判分由程序做，答案和错因标注在作答前不给学习者和主 agent 看；错因标不全就退回重写。 |
| `notes` 笔记 | `note-organizer.md` | 从学习者明确说过的话和可信的答题证据里整理学习笔记。 | 严格 JSON；只在开启学习记录后、整理笔记时运行并写入。 |

```text
请求 → 程序先处理：开场问答 / 记录开关 / 整理笔记 / 换一题 / 当前题判分
                                      │
                               coordinator decide
                                      │
            ┌─────────────────────────┴────────────────────────┐
            │ mode=answer                                      │ mode=tools
            ▼                                                  ▼
    四字段答复 + 内部任务控制                     检查派活理由与原文证据，算出执行顺序
            │                                                  │
            │                       程序搜索 → 核对（查证）  画图 → 核对（审图）  出题
            │                              按依赖等待，没有依赖的同时开工
            │                                                  │
            │                                  coordinator respond + 各能力终态
            │                                                  │
      校验并更新状态                                  最终校验 → 提交笔记证据 / 产物
            └─────────────────────────┬────────────────────────┘
                                      ▼
                     需要时：出题 agent 按回复出一道检查题
                                      ▼
                              完整四字段 JSON
```

首次模型调用使用 `phase=decide`。普通讲解、规划、闲聊或必要澄清可以在内部 `mode=answer` 中直接给出最终四字段答复和任务控制；校验通过就完成，不再固定走 `plan → respond` 两次调用。只有本轮不能直接回答完成时，才返回 `mode=tools` 的计划，执行选定能力后再调用 `phase=respond`。学习者对当前程序管理的选择题作答必须经过程序判分，不能用直答绕过进度更新。

### 执行顺序

主 agent 只说明内容上谁依赖谁（`visual_uses_research`、`quiz_uses_research`、`quiz_uses_visual`），顺序由程序决定：

- 画图或出题默认等本轮的检索和查证做完，除非主 agent 明确说不用检索结果。检索只在事实不确定或有时效时才跑，所以没写明的依赖按“有”处理。
- 出题只有在主 agent 明确说“看图答题”时才等图审过。
- 本轮不运行的能力上的等待会被去掉；等待只向前（检索 → 画图 → 出题），不会互相等死。
- 笔记不参与这里的调度，见第 4 节。

### 工具选择依据

模型侧没有注册函数工具；下表的“工具”是程序管理的能力。计划中的 `tool_reasons` 是四能力字典：未请求项为 null，请求项含枚举 `code` 和当前学习者消息中的 `evidence_quote`。程序检查理由和请求是否一致、引文是否来自当前输入，以及状态和权限条件；原文匹配不证明工具在语义上确有必要，这一项仍须模型行为验收。

引文通常至少含两个非空白字符；只有当前输入去掉首尾空白后本身是单字且完整引用时才允许单字，例如 A 或“好”。这一检查只过滤过短引文，不证明工具需求在语义上成立。

| 能力 | 允许的理由 code | 必要条件与执行边界 |
|---|---|---|
| 检索 | `explicit_lookup`、`time_sensitive`、`fact_check`、`knowledge_gap` | 明确查找、时效、核查或知识缺口；主 agent 在计划里写 2–4 条搜索词（`search_queries`），程序直接拿去搜索，没写时用检索问题本身搜；取得真实材料后才调用核对 agent。 |
| 测验 | `explicit_practice`、`accepted_check`、`continue_practice` | `start` 对应前两项，`continue` 必须对应 `continue_practice`；范围不足先澄清。一组练习默认 3 题，每次出一道，当前选择题答案由程序判分。 |
| 笔记 | `profile_update`、`learning_evidence`、`error_record`、`review_plan` | 必须已开启学习记录，并且本轮有相应信息或证据；教学回合里只记为待整理（`deferred`），笔记 agent 在整理笔记时才运行。 |
| 可视化 | `explicit_visual`、`comparison`、`process`、`function`、`active_recall` | 原理、机制和变化生成通用 scene；也支持 comparison / process / linear_function / flashcards / diagram；外观和场景可以用 illustration。需要图不自动意味着需要检索。 |

E/F/G、明确结束或暂停都不启动这些能力，分别先处理情绪、安全边界、澄清或停止。直答和工具路径的暂停都把 `next_step` 设为 null。当前等待题收到可以明确判分的作答时，除非暂停、结束或停止测验，否则必须走有效的 quiz continue；不能靠改标 E/F/G 或只选其他能力绕过判分。没有必要能力时直接回答或澄清。如果请求了检索但材料有争议或没核实完，不把它转成确定性的教学图。

对这类明确作答，如果 `decide` 已返回结构合法的直答，或工具计划没有有效选择 quiz continue，程序从已存的待答题重建最小续答计划，不额外调用 `decide`，也不因为选错路由让整轮进入 G。恢复计划使用 C / is_implicit=true，沿用已存的 topic、total、difficulty；participant 以已存身份为基础，有本轮工具计划时再保守合并：任一为 companion_report 就保留陪伴者身份，双方都是 learner 才保留 learner，缺失或 unknown 不升级。只继续判分，不检索、不画图。正常有效的续答不重建路由，但同样保守合并身份。

没有活动的待答题、答案有歧义时不强制恢复；暂停、结束和 stop 优先。`mode=tools` 中只有 `quiz_action=stop`、其他能力都未请求时也可以清理状态，不必为停止虚构需求。测验恢复还覆盖“声称练习（intent C）却直答”的情形：重建最小 quiz 计划（没有活动测验为 start，主题取答复的任务摘要；已有活动测验为 continue），同样不额外调用 `decide`。工具计划中 continue 却没有活动测验时降级为 start，缺少主题就补问，不整轮兜底。JSON/Schema 错误由 runner 做有限的格式重试，主 agent 或汇总调用的真实异常仍进入固定兜底。

### 完成依据与工具终态

直答的完成依据是最终答复通过结构与业务校验。工具路径只运行一条有界链路：检索、画图、出题取得结果或明确的失败状态后，交给 `respond` 说明结果和限制，再校验并提交。出题或主流程的致命错误进入固定兜底，不再调用 `respond` 补救；如果已交付先行解释，则保留这段文字并说明后续未完成，学习状态不继续推进。资料不足不会触发“把全部 agent 再跑一遍”的循环。笔记证据和产物在最终校验通过后才提交，不脱离本轮在后台写入。

`respond.tool_results` 是四能力状态字典；接纳回合的 `state.last_execution` 含 `mode`（answer/tools）和四能力字典 `tools`。二者都是内部字段，不加入公开响应，也不包含选择理由正文。

只有实际恢复路由的回合另记 `state.last_execution.routing_repaired`（待答误路由为 `"pending_quiz_answer"`，声称练习却直答为 `"practice_without_tools"`，明确要图却直答为 `"explicit_visual_without_tools"`）。视觉恢复只识别通用请求，不按知识点选择固定模板；排除拒绝、术语释义、模拟考试和演示文稿，并保留 E/F/G、暂停和结束优先。

| 能力 | 内部状态 |
|---|---|
| 通用 | 未选择为 `not_requested`；被策略禁止为 `skipped_policy`。 |
| research | `verified`、`likely`、`disputed`、`unavailable`。 |
| quiz | `question_ready`、`awaiting_answer`、`feedback_ready`、`finished`、`needs_input`。 |
| notes | 教学回合里为 `deferred`（留到整理笔记时）；整理时汇总前 `prepared`，新增写入成功后 `saved`，没有新增写入为 `not_written`，失败为 `failed`。 |
| visual | 汇总前 `ready`，发布成功后 `published`，没完成为 `unavailable`。 |

“prepared/ready”只表示可以在本轮使用，不能提前声称已保存或已发布；最终提交后的状态才写入已接纳的会话结果。

### 图文讲解与外部授权（2026-10-19）

普通问答仍由主 Agent 一次 `decide` 交付完整解释。是否画图取决于当前知识目标、已有基础、卡点和偏好：结构/过程/比较可自主选图；图没有教学增益时用文字。图与正文沿用同一例子、符号和观察目标，有效的看图任务保留在唯一 `next_step`，不再被测验邀请覆盖。

内部 tools decision 新增可空 `initial_explanation`，plan 新增 `visual_source`（local/web/generated/null）。公开四字段 JSON 不变。`LearningPipeline.run_stream` 仅在稳定知识、A/B、本地可视化、无检索/测验且没有待答题时发布已校验的 `explanation` 块；最终 `{output,state}` 合并这段不可变前缀及图示补充。无需额外模型调用。Space 用同一个气泡先显示解释，终态才提交历史、选项和图；关闭流会取消并等待工作任务，图或后续阶段失败不抹掉已显示解释。这是分阶段交付，不是逐 token 展示原始 JSON。

外部检索、网络搜图、外部生图默认未授权。模型收到 `capabilities` 的 available/authorized，配置了后端不代表许可。程序在执行前提出本次授权，保存原请求；批准后恢复原请求，拒绝后用已有知识与本地图解继续。授权仅覆盖本次原请求，支持分步批准，不能延续到新问题或另一会话。宿主已获取许可时可传入 `run(..., external_permissions={...})`；不得把模型字段直接当该参数。实际外部调用还有门禁；题目意外要求生图而无许可时改成纯文字题。当前未接入 MCP 能力，capabilities 中为不可用；新增适配器时须执行同样授权检查。原本配置的对话模型调用属于应用的基础服务，这个门禁约束额外的外部工具。

教学质量的真实模型验收见 [teaching-quality-cases.md](teaching-quality-cases.md)。离线回归验证程序规则，不代表各学科讲解正确性或图片效果已经通过真实模型评测。

## 2. 托管入口与输出协议

应用通过 Agent Framework 的 `BaseAgent` 接入 `ResponsesHostServer`。内部 agent 消息、搜索结果、校验失败的内容和笔记都不直接发给学习者。Foundry 托管入口仍等终态才发送结果，没有逐字或分阶段正文输出；本次分阶段交付接在 Space。托管入口会读取平台注入的用户编号（`x-agent-user-id`），作为学习记录的键；取不到时按会话保存。

托管协议的响应与历史显式使用内存存储，避免 SDK 默认把原始聊天写入用户目录；生命周期限于当前进程。服务器关闭 SDK 的自动遥测初始化，没有接入云端遥测或原始消息采集。

每次模型调用结束记录一条固定字段的 `agent_stage` 日志：role、phase（decide / quiz / respond / visual_review / specialist）、attempt、outcome（ok / invalid_output / provider_or_timeout_failure / cancelled）、estimated_input_tokens、模型返回的 input / output / cache_read token 数和 elapsed_ms，不含学习者正文、理由引文或工具材料。耗时只覆盖这一次模型调用，不是整轮耗时，也不含搜索的网络等待。同一条记录也会交给可选的阶段回调；演示网页用它显示“正在检索和查证资料…”这类进度提示。

所有 agent 的输出按各自的 Schema 校验。JSON/Schema 错误在调用次数和超时允许时最多重试一次；模型调用异常或超时重试一次，不走格式重试。每轮最多 12 次模型调用、180 秒。致命错误进入固定兜底，可选能力失败可以局部降级；工具链的最终意图必须和计划一致，直答只以自身 `response.intent` 为意图来源。`LEARNING_COACH_NATIVE_JSON=1` 可以请模型端按结构输出，本地校验始终执行；实际模型和端点是否完整支持，要在目标环境中验证。

每个 agent 用哪个模型部署由 `LEARNING_COACH_MODEL_<角色>` 决定，没设置的角色用 `AZURE_AI_MODEL_DEPLOYMENT_NAME`；当前分工见 [`architecture.md`](architecture.md#模型分工)。

最终响应保持四字段，定义以 [`response.schema.json`](../../agent/response.schema.json) 为准：

```json
{
  "schema_version": "1.0",
  "intent": {"primary": "B", "is_implicit": false},
  "message": "先想办法去掉等式左边的常数项，同时保持两边相等。",
  "next_step": {"kind": "question", "text": "等式两边应该同时做什么操作？"}
}
```

`next_step` 可以为 `null`。`is_implicit` 只描述主意图的识别方式。来源链接、互动图链接和题目配图链接都写在可见文本里，不增加顶层字段。

## 3. 检索与核查

实现文件：[`research.py`](../../agent/learning_coach/service/research.py)。接口为 `ResearchService(runner, provider)`，调用 `await service.run(question, queries)` 得到 `queries / sources / claims / overall / warnings`。`question` 是主 agent 写的检索问题，`queries` 是它在同一个计划里写的搜索词；程序去重、截断后直接搜索，一条都没有时用检索问题本身搜，并记一条 `queries_missing` 警告。写搜索词的规则在主 agent 的提示词里：第 1 条查概念或原理，第 2 条找权威资料或反例，有争议时才加第 3–4 条；措辞中性，按学段决定资料深浅，不写任何个人信息。

搜索词原来由单独的检索 agent 写。它只做这一件很小的事，却要多一次模型调用；主 agent 判断时本来就要写“查什么”，所以并了进去。

默认使用不需要密钥的 Wikipedia Action API：先搜索页面，再通过 TextExtracts 取纯文本节选。设置 `TAVILY_API_KEY` 时改用 Tavily，优先用它返回的页面原文，原文缺失时明确标为搜索摘要。Tavily 生成的 `answer` 不进入证据。实际使用 Tavily 可能产生该提供方的费用。

每次最多 4 个查询，每个查询最多 3 个结果；单次 API 响应最多 1 MiB，每个来源最多保留 3000 字。网络请求只发往固定的提供方 API 域名，拒绝重定向，不抓取用户或模型给出的任意网址。网络失败返回 `unavailable`，不编造结果。

程序建立来源编号、标题、网址、发布方和原始摘录；`excerpt_kind` 区分 `page_extract` 与 `search_snippet`。核对 agent 只能引用这些来源，程序再验证 `source_id`、逐字摘录和证据关系。

| 标记 | 含义 |
|---|---|
| `verified` | 核对 agent 判断至少两个独立发布方的页面节选直接支持这个说法，并通过程序的来源、摘录和独立性检查，没有已引用的反证。 |
| `likely` | 有相关支持，但只有一个发布方、只有搜索摘要，或无法确认独立性。 |
| `disputed` | 提交的有效证据中有反证，需要向学习者说明局限。 |
| `unavailable` | 没有足够证据、检索失败或核查无法完成。 |

Wikipedia 的不同语言版本算同一发布方。同域和子域、相同或近似复制的内容不算独立证据；未知的共同所有权和难以识别的转载仍依赖核对 agent 判断。`verified` 是本流水线的证据等级，不是对事实正确性的保证；数字、时效和复杂推理仍需要真实样本验收。

API 依据：[MediaWiki Search](https://www.mediawiki.org/wiki/API:Search)、[TextExtracts](https://www.mediawiki.org/wiki/Extension:TextExtracts#API)、[Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search)。

## 4. 画像、笔记与记忆

实现文件：[`onboarding.py`](../../agent/learning_coach/domain/onboarding.py)、[`memory.py`](../../agent/learning_coach/db/memory.py)、[`foundry_memory.py`](../../agent/learning_coach/adapters/foundry_memory.py)。

### 开场与记录开关

第一次见面，程序问三件事：谁在学（自己，还是家长或老师帮孩子用）、在哪个阶段、要不要保存学习进度。这几步不调用模型，只认识别得出的回答。可能未满 14 岁的学习者要家长确认才开启记录；还有几种情况没拦住（开场跳过、回访、演示模式），见 PRD 3.1。`LEARNING_COACH_ONBOARDING=0` 关闭开场问答（用于自动评测），`LEARNING_COACH_DEMO_MODE=1` 跳过问答、预设学段并开启记录（只用于演示）。

| 学习者说 | 效果 |
|---|---|
| “开启记录” 或 `/memory on` | 开始保存并读取学习记录。 |
| “关闭记录” 或 `/memory off` | 停止记录，并隐藏已有记录；不等于删除。 |
| “删除记录”，再回复“确定删除”；或 `/forget` | 删除这个学习者的全部记录和同意状态，恢复为关闭。 |

### 记录按谁保存

学习记录的键优先用学习者编号，没有时用会话编号：演示网页和命令行只有会话编号（页面关闭即失效），托管接口传平台注入的用户编号。陪伴者转述和学习者自述分别标明来源；没有证据的基础、关系和掌握程度保持未知。沉默、陪伴者代答、听过解释或说“懂了”都不算独立答对。

### 笔记什么时候整理

教学回合不派笔记 agent。程序每轮只记下判分证据，把本轮的学习信息标为待整理。整理发生在两种时候：

- 练完一组题后，程序问“要把这次学的整理成笔记吗？”，学习者回答“整理”；
- 学习者自己说“整理笔记”。

整理时，笔记 agent 收到上次整理之后学习者说过的话、标为待整理的回合、对话要点、练习结果（含疑似错因）、学习者卡片和已有学习记录。输出里的每一条都要引用学习者原话，程序逐字核对后用一个事务写入，按回合去重，失败时不提交半份笔记。

### 错因与掌握程度

错因分四类：概念性误解（`conceptual`）、步骤性跳跃（`step_skipping`）、审题偏差（`misreading`）、迁移失败（`transfer_failure`），确认不了的记为原因待确认（`unclassified`）。旧数据里的 procedural、careless、negative_transfer 在启动时分别转成步骤性跳跃、审题偏差、概念性误解。

答错时，程序按所选选项从出题 agent 的标注里取出“疑似错因”交给主 agent。学习者自己说出思路、和疑似一致，才算确认；整理笔记时，确认过的写明类型，没确认的记为原因待确认。同一个知识点、同一类疑似错因在本次对话里出现第二次，程序标记“反复出错”，主 agent 必须换一种讲法或回到前置知识。

模型不能直接写掌握程度。可信的独立作答把 `mastery_level` 从 0 起算：答对按难度加 0.1 / 0.15 / 0.2，答错减 0.15；这个知识点上一次判分也是同一类错因答错时，再多减 0.1。独立复述答对设诊断起点 0.3。这是少量观察得出的产品启发式指标，不是校准过的掌握度；非独立、陪伴者或来源未知的证据不改分。

### Foundry Memory 语义层

设置 `LEARNING_COACH_MEMORY_STORE` 时，核对过的笔记会再同步一份到 Foundry Memory：转成不含学习者原话的短句，每个学习者一个范围。读取学习记录时，程序把语义检索的结果作为单独一段带上，预算不够时先去掉这一段；检索没有结果时退回列表读取。SQLite 仍然是准的那一份，掌握程度、去重和同意状态都只看 SQLite；Foundry Memory 任何一步出错都退回只用 SQLite。存储需要先用 [`scripts/setup-memory.sh`](../../scripts/setup-memory.sh) 创建。

数据库可以持久保存，但账号鉴权、多设备同步、数据库迁移和云端备份不在当前范围。进程内的任务进度也不等于数据库里的学习记录；托管容器和 Space 的数据目录在 `/tmp`，重启会清空 SQLite，需要持久磁盘才能长期保留。

## 5. 出题

实现文件：[`quiz-writer.md`](../../agent/prompts/agents/quiz-writer.md)、[`contracts.py`](../../agent/learning_coach/domain/contracts.py)、[`pipeline.py`](../../agent/learning_coach/service/pipeline.py)。

两种题都由出题 agent 写：

- **练习题**：学习者要练习或接受检查时开始一组，默认 3 题，按“理解 → 应用 → 迁移”逐步加深。
- **检查题**：主 agent 讲完一块时在决定里附上 `check_question {topic, difficulty}`，出题 agent 根据这一轮刚写好的回复出一道。检查题只有一题，做完不问要不要整理笔记。

出题规则写在 `quiz-writer.md`：只考刚讲的那一块，学习者卡住的地方优先；题干按学段写；2–4 个选项，正确答案只有一个，位置要变化；每个错误选项标一次 `{choice_index, error_type, misconception}`，正确选项不标；解析要点出最可能选错的误解；不能和本组已出的题重复。

程序检查：答案位置超出选项数，或错误选项没有正好各标一次，就带着修改意见退回重写；质量问题最多退回两次。还不行时，练习题按出题失败处理，这一轮进入固定兜底回复；检查题出不来时，保留主 agent 原来的下一步。答案、解析和错因标注保存在程序里，作答前不给学习者，也不给主 agent。作答时只接受明确的选择（字母或选项原文），由程序判分；说不清选了哪个就不判分。

**什么时候配图**：只有看图能让题目更公平、更好懂，答案靠图里一眼就能认出的整体特征，而且不依赖精确的数量、数字、文字、尺寸或原理细节时，出题 agent 才请求配图。prompt 用英文写，图里不能有文字，图的说明（alt）不能透露答案。程序用 FLUX.2-pro 生成（60 秒超时），作为配图产物发布，回复里带“看题目配图”链接。生成失败时，程序带上 `image_unavailable` 让出题 agent 改出一道不用图的题。题目配图目前没有经过看图检查，学习者觉得图不对可以换题。

**换一题**：学习者说“换一题”“跳过这题”等，程序换掉当前这道，不判分；换题失败时保留原题。

## 6. 可视化产物

渲染实现：[`visuals.py`](../../agent/learning_coach/adapters/visuals.py)、[`scenes.py`](../../agent/learning_coach/adapters/scenes.py)。支持以下声明式 spec：

| `kind` | 内容 |
|---|---|
| `scene` | 按知识点生成的通用场景和多帧变化，可以逐帧观察和回看。 |
| `comparison` | 两个对象和逐项对照。 |
| `process` | 顺序步骤和说明。 |
| `flashcards` | 可以翻面的知识卡片。 |
| `linear_function` | 固定坐标轴探索一次函数，可以显式适配视窗和重置。 |
| `diagram` | 带文字标签的节点关系图。 |
| `illustration` | 外观或场景配图，用 FLUX.2-pro 生成。 |

模型只提供受约束的数据，不提供 HTML、JavaScript 或外部资源 URL。模板转义文案，交互用固定脚本；互动模板运行时不加载外部资源，illustration 在服务端调用生图。准备产物和提交分开，最终回复失败时不提前发布文件。

产物用随机能力 token，经 `/artifacts/{token}` 路由读取，有效期 24 小时。拿到链接的人在有效期内都能打开，它不是登录鉴权链接，内容应限于教学材料。演示网页把互动图和配图直接显示在页面里。托管网关不转发自定义路由，所以托管入口经 `LEARNING_COACH_ARTIFACT_BLOB_URL` 把同一份产物镜像到公开 blob 容器（匿名可读、不可列举、32 位不可猜 token、约 2 天生命周期删除），回复里给 blob 链接。

### 生成、校验与有界修正

原理、过程和变化由画图 agent 生成可观察的场景和多帧图解；不按知识点关键词匹配固定模板。照片、实物外观和场景配图可以选 illustration，但一张装饰性配图不能当作可操作的原理演示。

视觉链路依次是：`画图 agent 生成 → 程序验证 → 核对 agent 审阅方案 → 必要时重画一次 → 再验证和审阅 → 准备产物 → 最终回复校验 → 发布`。审阅用核对 agent 的独立调用（`phase=visual_review`），输入和协议都和查证资料分开。没通过时把明确的修改意见交回画图 agent；内容重画最多一次。最终仍没通过或依赖失败时不发布这张图，主 agent 说明没完成和可以继续的学习步骤。起草、审阅和重画共用 60 秒；超时不发布产物，把失败状态交回主流程继续作答。画图并发进行期间，每次模型调用都为最后的 `respond` 预留 2 次调用额度，额度不够时可选能力局部降级。

程序验证负责受支持的元素、字段类型和数量、坐标范围、同帧标识唯一、文字能否放下和标签重叠，以及隔离可执行内容；核对 agent 负责判断图是否回应学习目标、关键关系和分步变化是否连贯、有没有漏掉必要的标签和说明。**模型审阅不等于真实像素验收，也不保证事实正确。** 实际排版、文字可读性和按钮行为仍需要浏览器验证。

scene 是通用的交互逐帧图解，不是任意连续物理仿真引擎。程序用固定版本 SVG.js 3.2.0 绘制；库、许可证和校验和保存在 `adapters/vendor/`，产物嵌入所需脚本并使用内容哈希 CSP，操作时不依赖 CDN 或生图 API。绘图库：[SVG.js 官方文档](https://svgjs.dev/docs/3.2/)。

输入交接：画图 agent 收到按 token 预算截取的最近对话（默认 1000 token，最新几轮总会保留）、学习目标、核实过的资料、学习者卡片和对话要点。输出交接：主 agent 收到 `visual_context`，包含图的类型、标题、内容、实际可操作的地方、观察问题和适用边界；不会收到图片编码或网页代码。失败时收到 status=unavailable、失败原因和尝试次数，用来如实说明完成状态。

## 7. 本地运行与验证

全部配置项见 [`agent/.env.example`](../../agent/.env.example)，常用的如下：

| 配置 | 默认值 / 用途 |
|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | 必填。Foundry 项目地址。 |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | 必填。默认模型部署。 |
| `LEARNING_COACH_MODEL_<角色>` | 给单个 agent 指定模型部署。 |
| `LEARNING_COACH_DATA_DIR` | 默认 `agent/.learning-coach`；保存 `memory.sqlite3` 和 `artifacts` 目录。 |
| `LEARNING_COACH_PUBLIC_BASE_URL` | 默认 `http://localhost:8088`；用于生成产物链接，不会自动启动服务或配置公开路由。 |
| `LEARNING_COACH_MEMORY_STORE` | 设置后开启 Foundry Memory 语义层；不设置只用 SQLite。 |
| `LEARNING_COACH_ARTIFACT_BLOB_URL` | 托管入口的产物公开 blob 容器地址（`scripts/setup-artifact-blob.sh` 创建）；不设置时链接走本机 `/artifacts/{token}`（演示网页默认）。 |
| `LEARNING_COACH_CTX_*` | 各 agent 看到的上下文 token 预算，默认值见 `domain/context.py`。 |
| `LEARNING_COACH_NATIVE_JSON` | 默认不开启；设为 `1` 请模型端按结构输出，本地校验始终执行。 |
| `TAVILY_API_KEY` | 可选；配置后用 Tavily，没配置时用 Wikipedia。 |

从仓库根启动同一条真实流水线的命令行演示：

```bash
uv run --project agent --locked python agent/demo.py
```

演示需要可用的模型部署和 Azure 登录。命令行本身不启动 HTTP 产物服务；打开 `/artifacts/{token}` 链接需要本地托管服务正在运行。

从仓库根运行全部离线测试（共 321 个）：

```bash
uv run --project agent --locked --group dev python -m unittest discover -s tests -v
```

测试使用假的 agent runner、假的检索提供方、假的 Foundry Memory 客户端和临时存储，检验真实流水线和错误边界，不发送模型请求，也不读取真实凭据。离线测试通过说明程序行为符合断言，不代表教学内容、线上模型的选择、事实核查语义或部署链路已经验收。

上线前应分别完成：真实模型多轮验收、真实搜索端点的失败和限流验收、记录关闭和删除验收，以及公开 URL、持久磁盘和会话恢复验证。当前实现没有把子 agent 部署成独立服务。
