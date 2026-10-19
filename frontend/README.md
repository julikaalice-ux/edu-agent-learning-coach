# edu-agent 新前端（React + Vite + Tailwind）

给 edu-agent 演示用的新前端：电影感 Hero（视频循环背景）+ 自然元素配色 + 对话面板。

## 开发

```bash
npm install
npm run dev      # http://localhost:5173
```

## 结构

- `src/components/NavBar.tsx` — 顶部导航（衬线 logo + 菜单 + CTA）
- `src/components/Hero.tsx` — 视频背景无缝循环（useLoopedVideo）+ 大标题 + CTA
- `src/components/Chat.tsx` — 对话面板（自然元素卡片风格）
- `src/hooks/useLoopedVideo.ts` — rAF 渐入渐出循环视频
- `src/styles/fonts.css` — Instrument Serif（展示）+ Inter（正文）
- `src/styles/theme.css` — Tailwind 入口 + 自然元素 token + fade-rise 动画
- `src/lib/api.ts` — 后端接线占位

## 设计 token（自然元素，参考「打开画册」视觉参考库）

| 用途 | 值 |
|---|---|
| 背景 | `#f3f1eb`（paper） |
| 主文字 | `#202721`（ink） |
| 苔绿（链接/按钮） | `#2c6040`（moss）/ `#264f36`（moss-deep） |
| 鼠尾草（标签底） | `#e4eadf`（sage）/ `#d8e4d2`（sage-deep） |
| 灰绿（辅助文字） | `#62715f`（muted） |
| 描边 | `#d9ded3`（line）/ `#bbc5b5`（control） |
| 焦点环 | `#63836d`（focus） |

## 后端接线（待做）

对话面板当前是界面预览。正式接线两种方案：

1. **SSE 直连**：前端直连托管 agent 的 Responses 端点（需要 CORS + 令牌处理，生产要经代理）；
2. **小代理**：一个无状态代理（如 Cloudflare Worker / 本地 FastAPI）转发到
   `https://<账户>.services.ai.azure.com/api/projects/edu-agent/agents/<agent>/endpoint/protocols/openai/responses`，
   前端只和代理说话。

接口形状见 `src/lib/api.ts` 的 `TurnRequest` / `TurnResponse`。
