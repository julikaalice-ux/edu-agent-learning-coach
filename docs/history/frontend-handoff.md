# 前端交接说明（给写前端的模型/开发者）

本文档是学习教练 agent 前端的唯一必读材料。前端是**纯静态页面**，不接触、也不需要读 agent 框架的任何代码（`learning_coach/`、`main.py`、pipeline 等）。

## 架构一句话

浏览器前端 →（二选一）→ 已部署在 Azure Foundry 的 agent。前端是**纯静态页面**，不接触、也不需要读 agent 框架的任何代码（`learning_coach/`、`main.py`、pipeline 等）。

- **生产模式（HR 演示）**：静态托管（GitHub Pages / Vercel 等）→ 微软账号登录（MSAL）→ 直连 Foundry 端点，无代理
- **开发模式（可选）**：本地代理（`web/proxy.py`，已写好）代持凭据，免登录
- **本地演示模式**：本地跑 agent + 本地聊天页（`web/serve.py` + `web/index.html`），免登录，**互动图链接可点开**（见下文）

前端把取令牌抽象成一个 `getToken()`：生产走 MSAL，开发走代理，其余代码完全一样。

## 认证与端点（生产模式）

MSAL.js 配置：

- authority：`https://login.microsoftonline.com/<tenant-id>`
- clientId：`575f118c-727b-47e4-8b85-c4e1591a7f96`
- redirectUri：开发 `http://localhost:5173`；**生产 URL 确定后必须加进应用注册的重定向列表**（找运维或我，一条命令的事）
- 登录方式：`acquireTokenPopup`，scopes：`["https://ai.azure.com/.default"]`

拿到令牌后，直连 Foundry 端点（已验证支持 CORS）：

```
Base URL: https://<ai-account>.services.ai.azure.com/api/projects/edu-agent/prompts/agents/agent-framework-agent-with-local-tools-responses/endpoint/protocols/openai
每个请求带：Authorization: Bearer <token>，查询参数 api-version=v1
```

开发模式则把 Base URL 换成 `http://localhost:8787`、不用 Authorization 头（代理会注入）。

## 本地演示模式（互动图可点开）

云端部署的互动图链接打不开（Foundry 网关不透传 agent 的自定义 `/artifacts/` 路由，实测 4 种路径均 404）。要演示互动图，本地跑 agent：

终端 1（agent，端口 8088，含 `/artifacts/{token}` 路由）：

```bash
cd ~/foundry-demo/agent-framework-agent-with-local-tools-responses
uv run --project agent --locked \
  python agent/main.py
```

终端 2（聊天页，端口 8600，代理转发 `/chat` → `http://127.0.0.1:8088/responses`，无状态，每轮带全量历史）：

```bash
python3 web/serve.py
```

打开 http://localhost:8600 即可。回复里的 `[打开互动图](http://localhost:8088/artifacts/...)` 链接在本机可正常打开（已端到端验证：出题 → 返回链接 → GET 200 HTML）。`web/proxy.py` 也支持 `LEARNING_COACH_PROXY_TARGET=local` 转发到本地 agent，供开发模式使用。

## 运行方式（开发）

终端 1（代理，可选）：

```bash
cd ~/foundry-demo/agent-framework-agent-with-local-tools-responses
uv run --project agent --locked python web/proxy.py
```

终端 2：前端用你喜欢的任意方式起（vite、静态服务器均可）。代理已带 CORS 头，任意 origin 都能调。

## API（只有两个端点）

以下路径相对上面的 Base URL。

### 1. 创建会话

`POST {Base URL}/conversations`，body `{}`。返回：

```json
{"id": "conv_xxxx", "object": "conversation", "metadata": {}, "created_at": 1791447131}
```

**`id` 是会话的唯一钥匙**。服务端持有全部状态（测验进度、学习记忆），前端必须无状态：只保管这个 id，每轮带上。"新开一个对话"按钮 = 重新创建 conversation。

### 2. 发送一轮消息

`POST {Base URL}/responses`

```json
{"conversation": "<上一步的 id>", "input": "用户的纯文本"}
```

返回的 JSON 取 `output` 数组中 `type == "message"` 的项，文本在 `content[0].text`。一轮只会有一个这样的 message。示例：

```json
{
  "output": [
    {"type": "message", "role": "assistant",
     "content": [{"type": "output_text", "text": "分数表示把整体平均分成几份后，取了其中的几份。"}]}
  ],
  "status": "completed"
}
```

非 200 响应：显示"这一轮没有完成，请重试"，保留用户输入。超时阈值建议 120 秒（出题轮较慢）。

## 消息呈现规则

1. **原文渲染**。服务端返回的是最终学习者文本（讲解 + 追问/题面已拼好），前端不做任何改写、不解析 JSON 结构
2. **Markdown 链接可点**：消息里会有 `[打开互动图](url)` 和 `[标题](来源url)` 形式的链接，渲染成可点的
3. **quiz 选项按钮化**：出题时文本里有这样的行：
   ```
   下面哪个分数最大？
   A. 1/2
   B. 1/3
   C. 1/4
   ```
   用正则 `^[A-D]\.\s`（多行模式）识别这些行，渲染成按钮；**点击按钮发送字母**（如 `A`）。不要把识别逻辑泛化到普通文本，也不要在用户已作答后继续显示按钮
4. **slash 命令**：以 `/` 开头的输入（`/memory on`、`/memory off`、`/forget`）原样发送，回复也是普通文本

## 明确禁止（这些是服务端的职责，前端碰了就是 bug）

- 不出题、不判分、不判断对错——判分在服务端，回复文本里直接有"你选对了"这类反馈
- 不保存 quiz 状态、不记"当前第几题"
- 不解析任何 JSON wire 协议（接口里没有）
- 不做用户身份、不记掌握度

## 已知边界（不用管，不是 bug）

- 一轮一个完整文本，**无流式输出**
- 偶发"这一轮没有完成"是服务端对模型异常的正常兜底，重试即可
- 互动图链接指向部署环境的产物地址（`http://localhost:8088/artifacts/...`），**云端/HR 模式打不开**（Foundry 网关不透传自定义路由；将来需把 ArtifactStore 换 blob 存储才能解，当前不做）；**本地演示模式可正常打开**，见上文「本地演示模式」
- 旧版协议原型已被 `web/serve.py` + `web/index.html` 重写覆盖，当前这两个文件就是本地演示模式的正式实现

## 设计建议（非约束）

- 对话式气泡 UI；用户消息右、教练消息左
- 出题轮可以把整道题渲染成卡片（题干 + 选项按钮组）
- "新对话"按钮重置 conversation id；"清空记录"不需要（刷新即可）
