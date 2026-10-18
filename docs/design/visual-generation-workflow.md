# 生图工作流梳理（教学图 / AI 配图 / 题目配图）

> 整理时间 2026-10-19。覆盖「互动教学图」「AI 配图」「题目配图」三条链路的代码、prompt、环境变量与已知失败点，供排查与修改对照。

## 总览：三条链路

```
链路 A：互动教学图（程序渲染，主要形态）
  编排者(coordinator) decide 填 visual_request
    → VisualGenerationService.generate (service/visual_generation.py)
        → visualizer 出声明式规格 (VISUAL_SCHEMA, prompts/agents/visual-designer.md)
        → 程序校验 + 本地渲染 (adapters/visuals.py, scenes.py, SVG.js)
        → verifier 独立审阅 (prompts/agents/source-check.md 的 visual_review 段)
        → 通过才发布 artifact（最多重画 1 次，共 2 稿）
    → 回复附 [打开互动图](artifact_url)，Space 页面内嵌 iframe 显示

链路 B：AI 配图（像素图，仅 illustration kind）
  visualizer 选 illustration → 程序调 generate_image (adapters/illustrate.py)
    → FLUX-2-pro（Foundry，默认）或 pollinations（备用）
    → 字节进 artifact，回复内 data:image 内联显示

链路 C：题目配图（出题请求图片时）
  quizzer 出题带 image 字段 → pipeline._question_picture (service/pipeline.py)
    → 同样走 generate_image → 回复带 [看题目配图]
```

## 文件清单与职责

| 文件 | 职责 |
|---|---|
| `service/visual_generation.py` | 生图编排：设计 → 校验 → 渲染 → 审阅 → 发布，最多 2 稿，90 秒总时限 |
| `prompts/agents/visual-designer.md` | visualizer 的 prompt：选型规则 + 各 kind 要点 + 输出协议（全文见下） |
| `prompts/agents/source-check.md`（visual_review 段） | 审阅 prompt：五项检查（request_match / factual_consistency / labels_clear / interaction_useful / not_misleading） |
| `domain/visual_contracts.py` | VISUAL_SCHEMA：8 种 kind 的声明式协议（comparison/process/flashcards/linear_function/scene/illustration/diagram/…） |
| `domain/teaching_visuals.py` | visual_handoff：规格 → 交给 responder 的说明与 limitations |
| `domain/visual_quality.py` | 审阅校验：validate_visual_review、needs_explanatory_visual（拦截"讲机制却要像素图"） |
| `adapters/visuals.py` | 程序渲染器：把声明式规格渲染成 HTML+SVG（固定版本 SVG.js 在 `adapters/vendor/`） |
| `adapters/scenes.py` + `scene_assets/` | scene kind 的逐帧渲染（scene.html/js/css） |
| `adapters/illustrate.py` | 图像后端：FLUX（默认）/ pollinations（备用），返回 PNG/JPEG 字节 |
| `db/artifacts.py` | 产物存储：token 化 URL、24 小时过期、会话删除 |
| `service/pipeline.py::_question_picture` | 题目配图入口（出题 → 生图 → artifact） |
| `server.py::_artifact_store` | 组装点：本机 /artifacts 路由（Space）或公开 blob（托管端 `LEARNING_COACH_ARTIFACT_BLOB_URL`） |
| `space/app.py::iframe_for / respond` | 演示页显示：互动图 iframe 内嵌，配图 data:image 内联 |

## 关键流程（代码级）

1. **选型**：coordinator 的 `visual_request` 决定是否画图；visualizer 按「依次检查」选 kind（flashcards → linear_function → comparison → scene → process → diagram → illustration）。
2. **校验**：`validate(spec, VISUAL_SCHEMA)`；渲染失败把渲染器错误码翻译成中文修正意见（`_RENDER_CORRECTIONS`）带回去重画。
3. **拦截规则**：`needs_explanatory_visual(goal, learner_text)` —— 讲机制/因果/参数变化时禁止 illustration（静态像素图讲不了机制），改为 scene/process/diagram。
4. **审阅**：verifier 收到 spec + research + render_check=passed；`validate_visual_review` 检查审阅自洽性和来源；verdict=pass 才发布。
5. **发布**：`artifacts.prepare(session_id, spec, image=…)` → token URL；外层事务通过后才 `artifacts.commit`（校验失败不发布）。
6. **显示**（演示页）：`respond()` 解析 `[打开互动图](url)` → `PIPELINE.artifacts.get(token)` → 含 `data:image` 的当配图内联、否则 iframe 内嵌。托管端经公开 blob 容器提供（链接匿名可读、2 天生命周期删除）。

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `LEARNING_COACH_IMAGE_BACKEND` | `flux` / `pollinations` | `flux` |
| `LEARNING_COACH_FLUX_DEPLOYMENT` | FLUX 部署名 | `flux-2-pro` |
| `LEARNING_COACH_FLUX_ACCOUNT_ENDPOINT` | 账号端点（默认从 FOUNDRY_PROJECT_ENDPOINT 推导） | — |
| `LEARNING_COACH_ARTIFACT_BLOB_URL` | 托管端公开 blob 容器地址；不设 = 本机 /artifacts 路由 | — |
| `LEARNING_COACH_MODEL_VISUALIZER` / `_VERIFIER` | 画图/审图用更强的模型 | 默认模型 |

## Visual designer prompt 全文

见 [`agent/prompts/agents/visual-designer.md`](../../agent/prompts/agents/visual-designer.md)，要点：

- 角色：声明式教学图卡设计员，不出网页代码、不补造事实。
- 事实依据：research=verified 才画具体数值/关系；research=null 只画稳定基础知识。
- 选型依次检查（以教学目的为准；用户说「图片」不等于像素图）。
- 各 kind 要点（scene 逐帧、diagram 矢量、illustration 的 prompt 是英文绘制指令且不含文字标签……）。
- 输出：单一 VISUAL_SCHEMA JSON，纯文本、无围栏、无 URL/HTML/JS。

## 已知失败点与排查

1. **FLUX 配额满**：francecentral 区 `FLUX.2-pro` 部署槽位 15/15 已满（az cognitiveservices usage list 可见）。现有部署可用，但新建部署会被拒；若生图报错先查日志的 `quiz_picture_unavailable` / `render_unavailable`。
2. **pollinations 备用**：`LEARNING_COACH_IMAGE_BACKEND=pollinations` 时走公共端点，质量低且带水印，只适合离线演示。
3. **配图不加文字**：`with_no_text()` 在 prompt 尾部强制 "No text, letters..."，防止 AI 图里的错误文字（尤其中文）。
4. **审阅不看像素**：review_scope=specification_only、pixel_reviewed=false——规格对不代表图对，静态配图不作原理证据（产品已声明）。
5. **演示页显示路径**：互动图走 artifact token；本地/魔搭/HF 用 `/artifacts/{token}` 路由，托管端走公开 blob。若托管端链接 404，先查 blob 容器权限/生命周期。
6. **生图评测**：`evals/visual_quality.py`（`runs/visual-*` 是它的输出）：9 个视觉用例 × 3 次，最近一轮整体成功率 26%（0 条全过、5 条时好时坏、4 条全挂），耗时中位 49s——生图链路的稳定性就是当前最弱的一环。
