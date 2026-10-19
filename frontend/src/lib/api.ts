/** 与后端对话的客户端与前端会话模型。 */

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  at: number;
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
  options: string[]; // 教练最新一轮的编号选项
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

export interface TurnRequest {
  conversation: string | null;
  input: string;
}

export interface TurnResponse {
  conversation: string;
  text: string;
  options: string[];
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
