import { useEffect, useRef, useState } from "react";
import { GreetingScene } from "./components/GreetingScene";
import { ChatScene } from "./components/ChatScene";
import { emptySession, streamTurn, type ChatMessage, learningAction, type LearningAction, type LearningReply, type Session } from "./lib/api";

const WELCOME: ChatMessage = {
  role: "assistant",
  content: "你好，可以问我任何你想学的知识",
  at: Date.now(),
};

// Compatibility with older hosted versions: hide the fixed capability pitch.
// A retry stays in the same conversation and never opens a second welcome bubble.
const INTRO_START = "你好，我是你的学习教练";
function replyMessages(text: string): ChatMessage[] {
  text = text.trimStart();
  if (text.startsWith(INTRO_START)) {
    const cut = text.indexOf("\n\n");
    text = cut < 0 ? "" : text.slice(cut + 2).trimStart();
  }
  return [{ role: "assistant", content: text, at: Date.now() }];
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
  const [retryInput, setRetryInput] = useState<string | null>(null);
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

  const [learningBusy, setLearningBusy] = useState(false);
  const [learningMessage, setLearningMessage] = useState("");
  const learningLock = useRef(false);
  const applyLearning = (reply: LearningReply) => {
    setSession(prev => ({ ...prev, conversation: reply.conversation,
      ...(reply.view ? { mistakes: reply.view.mistakes, notes: { cards: reply.view.study_notes },
        settings: { ...prev.settings, recordsOn: reply.view.enabled } } : {}) }));
  };
  const performLearning = async (action: LearningAction) => {
    if (learningLock.current || thinking) throw new Error("请等待当前操作完成。");
    learningLock.current = true; setLearningBusy(true); setLearningMessage("");
    try {
      const result = await learningAction(session.conversation, action);
      applyLearning(result); setLearningMessage(result.message || "");
      return result;
    } catch (error) {
      setLearningMessage(error instanceof Error ? error.message : "记录操作失败");
      throw error;
    } finally { learningLock.current = false; setLearningBusy(false); }
  };
  const send = async (textOverride?: string) => {
    const text = (textOverride ?? input).trim();
    if (!text || thinking || learningLock.current) return;
    enter();
    setInput("");
    setThinking(true);
    setRetryInput(null);
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

      // Legacy hosted protocol has no status field: recognize only its fixed failure text.
      const failed = replyMessages(reply.text)[0].content.startsWith("这次处理没有顺利完成");
      if (failed) setRetryInput(text);
      setSession((prev) => {
        const messages = withReply(prev.messages, failed
          ? "这次处理没有顺利完成。可以重试刚才的问题，或继续提问。" : reply.text);
        return {
          ...prev,
          conversation: reply.conversation,
          options: reply.options,

          messages,
        };
      });
      if (failed) return;
      try {
        applyLearning(await learningAction(reply.conversation, { action: "read" }));
      } catch {
        setLearningMessage("回复已完成，但学习记录刷新失败。请在笔记页面重试刷新。");
      }
    } catch {
      setRetryInput(text);
      setSession(prev => ({ ...prev, messages: withReply(prev.messages,
        "连接暂时中断。可以重试刚才的问题，或继续提问。") }));
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
          thinking={thinking || learningBusy}
          onRetry={retryInput ? () => void send(retryInput) : undefined}
          learningMessage={learningMessage}
          onLearning={performLearning}
          input={input}
          onInput={setInput}
          onSend={(text) => void send(text)}
          onLogoClick={goHome}
          onSettingsChange={(settings) => {
            if (settings.recordsOn !== session.settings.recordsOn) {
              void performLearning({ action: "records", enabled: settings.recordsOn }).catch(() => {});
            } else setSession(prev => ({ ...prev, settings }));
          }}
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
