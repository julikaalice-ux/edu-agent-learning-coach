/** 与后端对话的客户端与前端会话模型。 */

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  at: number;
  kind?: "intro"; // 功能讲解对话框（教练首次回复时展示能力清单）
}

export interface Mistake {
  concept: string;
  category: string; // 概念性误解 / 步骤性跳跃 / 审题偏差 / 迁移失败
  question: string;
  at: number;
}

export interface SessionNotes {
  profile: { label: string; value: string }[];
  concepts: { label: string; status: string }[];
  errors: Mistake[];
}

export const STAGE_OPTIONS = ["学龄前", "小学", "初中", "高中", "大学", "已工作", "不想说"] as const;
export const WHO_OPTIONS = ["我自己", "我是家长或老师，帮孩子用"] as const;

export interface SessionSettings {
  who: string; // 谁在学（开场问题 1）
  stage: string; // 学段（开场问题 2）
  recordsOn: boolean; // 是否保存学习进度（开场问题 3，本次会话有效）
}

/** 前端会话状态：全部内存，页面关闭即消失。 */
export interface Session {
  conversation: string | null; // 托管 agent 会话 id（本次页面内使用）
  messages: ChatMessage[];
  options: OptionCard[]; // 教练最新一轮的选项卡片
  mistakes: Mistake[];
  notes: SessionNotes;
  settings: SessionSettings;
}

export function emptySession(): Session {
  return {
    conversation: null,
    messages: [],
    options: [],
    mistakes: [],
    notes: { profile: [], concepts: [], errors: [] },
    settings: { who: WHO_OPTIONS[0], stage: "未设置", recordsOn: false },
  };
}

export interface OptionCard {
  key: string; // A/B/C/D 或编号；「跳过」为空
  text: string;
}

export interface TurnRequest {
  conversation: string | null;
  input: string;
}

export interface TurnResponse {
  conversation: string;
  text: string;
  options: OptionCard[];
}

/** 经 Vite 代理转发到本地 proxy（backend/proxy.py → 托管 agent）。 */
export async function sendTurn(request: TurnRequest): Promise<TurnResponse> {
  const response = await fetch("/turn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error((body as { error?: string }).error || `http_${response.status}`);
  }
  return (await response.json()) as TurnResponse;
}

/** 流式：onDelta 逐段收到回复文本，resolve 时返回完整结果。 */
export async function streamTurn(
  request: TurnRequest,
  onDelta: (text: string) => void,
): Promise<TurnResponse> {
  const response = await fetch("/turn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...request, stream: true }),
  });
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => ({}));
    throw new Error((body as { error?: string }).error || `http_${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: TurnResponse | null = null;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const line = frame.split("\n").find((item) => item.startsWith("data:"));
      if (!line) continue;
      try {
        const payload = JSON.parse(line.slice(5).trim());
        if (payload.delta) onDelta(payload.delta);
        if (payload.done) result = payload;
      } catch {
        /* 忽略不完整的帧 */
      }
    }
  }
  if (!result) throw new Error("stream_incomplete");
  return result;
}
