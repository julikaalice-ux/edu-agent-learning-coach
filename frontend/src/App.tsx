import { useEffect, useRef, useState } from "react";
import { GreetingScene } from "./components/GreetingScene";
import { ChatScene } from "./components/ChatScene";
import { emptySession, streamTurn, type ChatMessage, type Mistake, type Session } from "./lib/api";

const ERROR_CATEGORIES = ["概念性误解", "步骤性跳跃", "审题偏差", "迁移失败"];

/** 从教练回复里提取错题（答错反馈）与笔记（整理完成）信号。 */
function extractLearning(text: string, messages: ChatMessage[]) {
  const mistakes: Mistake[] = [];
  const category = ERROR_CATEGORIES.find((item) => text.includes(item)) ?? "";
  if ((category || text.includes("选错了") || text.includes("答错了")) && messages.length) {
    const answer = [...messages].reverse().find((item) => item.role === "user");
    const question = [...messages]
      .reverse()
      .find((item) => item.role === "assistant" && /[A-D]\.\s/.test(item.content));
    mistakes.push({
      concept: question ? question.content.slice(0, 24) : answer?.content.slice(0, 24) ?? "本次练习",
      category: category || "原因待确认",
      question: question?.content ?? answer?.content ?? "",
      at: Date.now(),
    });
  }
  let noteLabel = "";
  if (text.includes("整理好了")) {
    const topic = [...messages]
      .reverse()
      .find((item) => item.role === "user" && /考|练习|复习/.test(item.content));
    noteLabel = topic ? topic.content.slice(0, 24) : "本次整理的知识点";
  }
  return { mistakes, noteLabel };
}

const WELCOME: ChatMessage = {
  role: "assistant",
  content: "你好，可以问我任何你想学的知识",
  at: Date.now(),
};

// 第一次见面就提问时，教练回复的开头是程序固定的自我介绍（以这句开头），
// 和回答之间空一行。页面把它拆出来单独放一个对话框，样式和回答一样。
// 这段介绍在模型开始写之前就会先到，回答还没来时只显示介绍。
const INTRO_START = "你好，我是你的学习教练";

/** 一轮回复对应的对话框：有自我介绍时是介绍 + 回答两个，否则一个。 */
function replyMessages(text: string): ChatMessage[] {
  const at = Date.now();
  const cut = text.indexOf("\n\n");
  if (!text.startsWith(INTRO_START) || cut < 0) return [{ role: "assistant", content: text, at }];
  return [
    { role: "assistant", content: text.slice(0, cut), at },
    { role: "assistant", content: text.slice(cut + 2).replace(/^\n+/, ""), at },
  ];
}

/** 用这一轮的回复替换最后一条用户消息之后的对话框（边写边显示时反复调用）。 */
function withReply(messages: ChatMessage[], text: string): ChatMessage[] {
  const lastUser = messages.map((item) => item.role).lastIndexOf("user");
  return [...messages.slice(0, lastUser + 1), ...replyMessages(text)];
}

/**
 * 单页两场景：问候（视频开场，只播一次）→ 镜头推入 → 对话（纯色）。
 * 点「学习教练」回到问候页；滚轮/上滑在问候页推入对话，之后只滚聊天区。
 */
export default function App() {
  // ?enter=1 直达对话场景（分享直链 / 调试用）
  const [entered, setEntered] = useState(
    () => new URLSearchParams(window.location.search).get("enter") === "1",
  );
  const [session, setSession] = useState<Session>(() => ({
    ...emptySession(),
    messages: [WELCOME],
  }));
  const [input, setInput] = useState("");
  const [thinking, setThinking] = useState(false);
  const wheelArmed = useRef(entered);

  const enter = () => {
    setEntered(true);
    wheelArmed.current = true;
  };

  const goHome = () => {
    setEntered(false);
    wheelArmed.current = false;
  };

  // 问候场景：滚轮向下（或触屏上滑）一次即推入对话。进入后不再响应。
  useEffect(() => {
    if (entered) return;
    const onWheel = (event: WheelEvent) => {
      if (event.deltaY > 0 && !wheelArmed.current) {
        wheelArmed.current = true;
        enter();
      }
    };
    const touch = { startY: 0 };
    const onTouchStart = (event: TouchEvent) => {
      touch.startY = event.touches[0]?.clientY ?? 0;
    };
    const onTouchEnd = (event: TouchEvent) => {
      const changed = event.changedTouches[0];
      if (!wheelArmed.current && changed && touch.startY - changed.clientY > 40) {
        wheelArmed.current = true;
        enter();
      }
    };
    window.addEventListener("wheel", onWheel, { passive: true });
    window.addEventListener("touchstart", onTouchStart, { passive: true });
    window.addEventListener("touchend", onTouchEnd, { passive: true });
    return () => {
      window.removeEventListener("wheel", onWheel);
      window.removeEventListener("touchstart", onTouchStart);
      window.removeEventListener("touchend", onTouchEnd);
    };
  }, [entered]);

  const send = async (textOverride?: string) => {
    const text = (textOverride ?? input).trim();
    if (!text || thinking) return;
    enter();
    setInput("");
    setThinking(true);
    // 图片类请求在等待生成时给一个「正在生成中」对话框；真实回复开始流入时会被替换。
    const wantsVisual = /(图|画|照片|配图|可视化|示意图|图解|图片)/.test(text);
    setSession((prev) => ({
      ...prev,
      options: [],
      messages: [
        ...prev.messages,
        { role: "user", content: text, at: Date.now() },
        ...(wantsVisual
          ? [{ role: "assistant" as const, content: "正在生成中，预计1分钟左右", at: Date.now() }]
          : []),
        { role: "assistant", content: "", at: Date.now() },
      ],
    }));
    try {
      let streamed = "";
      const reply = await streamTurn(
        { conversation: session.conversation, input: text },
        (delta) => {
          streamed += delta;
          const shown = streamed;
          setSession((prev) => ({ ...prev, messages: withReply(prev.messages, shown) }));
        },
      );
      const { mistakes, noteLabel } = extractLearning(reply.text, session.messages);
      setSession((prev) => {
        const messages = withReply(prev.messages, reply.text);
        const notes = noteLabel
          ? { ...prev.notes, concepts: [...prev.notes.concepts, { label: noteLabel, status: "刚整理" }] }
          : prev.notes;
        return {
          ...prev,
          conversation: reply.conversation,
          options: reply.options,
          mistakes: [...prev.mistakes, ...mistakes],
          notes,
          messages,
        };
      });
    } catch (error) {
      setSession((prev) => {
        const messages = [...prev.messages];
        const last = messages[messages.length - 1];
        messages[messages.length - 1] = {
          ...last,
          content: `这次连接没有成功（${error instanceof Error ? error.message : "未知错误"}），请稍后再试。`,
        };
        return { ...prev, messages };
      });
    } finally {
      setThinking(false);
    }
  };

  return (
    <div className="relative h-dvh w-full overflow-hidden bg-paper">
      {/* 问候场景 */}
      <div
        className={`absolute inset-0 transition-all duration-700 ease-in-out ${
          entered ? "pointer-events-none scale-110 opacity-0" : "scale-100 opacity-100"
        }`}
      >
        <GreetingScene input={input} onInput={setInput} onSend={() => void send()} />
      </div>
      {/* 对话场景：镜头推入 */}
      <div
        className={`absolute inset-0 transition-all duration-700 ease-in-out ${
          entered ? "scale-100 opacity-100" : "pointer-events-none scale-95 opacity-0"
        }`}
        aria-hidden={!entered}
      >
        <ChatScene
          messages={session.messages}
          options={session.options}
          mistakes={session.mistakes}
          notes={session.notes}
          settings={session.settings}
          thinking={thinking}
          input={input}
          onInput={setInput}
          onSend={(text) => void send(text)}
          onLogoClick={goHome}
          onSettingsChange={(settings) => setSession((prev) => ({ ...prev, settings }))}
        />
      </div>
      {/* 场景指示器 */}
      <div
        className={`absolute bottom-8 left-1/2 z-20 -translate-x-1/2 transition-opacity duration-500 ${
          entered ? "opacity-0" : "opacity-100"
        }`}
      >
        <div className="animate-bounce text-xs tracking-widest text-muted">向下滚动进入对话</div>
      </div>
    </div>
  );
}
