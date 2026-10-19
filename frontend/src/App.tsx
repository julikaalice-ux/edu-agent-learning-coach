import { useEffect, useRef, useState } from "react";
import { GreetingScene } from "./components/GreetingScene";
import { ChatScene } from "./components/ChatScene";
import { emptySession, sendTurn, type ChatMessage, type Session } from "./lib/api";

const WELCOME: ChatMessage = {
  role: "assistant",
  content: "你好，可以问我任何你想学的知识",
  at: Date.now(),
};

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
    setSession((prev) => ({
      ...prev,
      options: [],
      messages: [...prev.messages, { role: "user", content: text, at: Date.now() }],
    }));
    try {
      const reply = await sendTurn({ conversation: session.conversation, input: text });
      setSession((prev) => ({
        ...prev,
        conversation: reply.conversation,
        options: reply.options,
        messages: [
          ...prev.messages,
          { role: "assistant", content: reply.text, at: Date.now() },
        ],
      }));
    } catch (error) {
      setSession((prev) => ({
        ...prev,
        options: [],
        messages: [
          ...prev.messages,
          {
            role: "assistant",
            content: `这次连接没有成功（${error instanceof Error ? error.message : "未知错误"}），请稍后再试。`,
            at: Date.now(),
          },
        ],
      }));
    } finally {
      setThinking(false);
    }
  };

  return (
    <div className="relative h-screen w-full overflow-hidden bg-paper">
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
