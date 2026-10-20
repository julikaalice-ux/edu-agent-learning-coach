import { useState, type ReactNode } from "react";
import type { ChatMessage, Mistake, OptionCard, SessionNotes, SessionSettings } from "../lib/api";
import { STAGE_OPTIONS, WHO_OPTIONS } from "../lib/api";

/** 互动图 / 题目配图 / 普通链接 / 纯文本的消息渲染。 */
const PART_PATTERN = /\[(打开互动图|看题目配图)\]\(\/artifact\/([A-Za-z0-9_-]+)\)|\[([^\]]+)\]\(([^)]+)\)/g;

function MessageContent({ text }: { text: string }) {
  // Visual links from a local agent or from the hosted agent's public storage
  // become this page's /artifact/<token> route, which serves either.
  const normalized = text
    .replace(/http:\/\/localhost:8088\/artifacts\//g, "/artifact/")
    .replace(/https:\/\/[a-z0-9]+\.blob\.core\.windows\.net\/artifacts\/([A-Za-z0-9_-]+)\.html/g, "/artifact/$1");
  const nodes: ReactNode[] = [];
  let last = 0;
  let key = 0;
  PART_PATTERN.lastIndex = 0;
  for (let match = PART_PATTERN.exec(normalized); match; match = PART_PATTERN.exec(normalized)) {
    if (match.index > last) {
      nodes.push(<span key={key++}>{normalized.slice(last, match.index)}</span>);
    }
    if (match[1]) {
      nodes.push(
        match[1] === "打开互动图" ? (
          <iframe
            key={key++}
            src={`/artifact/${match[2]}`}
            title="互动图"
            sandbox="allow-scripts"
            className="mt-2 block h-[460px] w-full rounded-xl border border-line bg-white"
          />
        ) : (
          <img
            key={key++}
            src={`/artifact/${match[2]}/image`}
            alt="题目配图"
            className="mt-2 block max-h-80 rounded-xl border border-line bg-white"
          />
        ),
      );
    } else if (match[3] && match[4]) {
      nodes.push(
        <a
          key={key++}
          href={match[4]}
          target="_blank"
          rel="noreferrer"
          className="text-moss underline underline-offset-4"
        >
          {match[3]}
        </a>,
      );
    }
    last = match.index + match[0].length;
  }
  if (last < normalized.length) {
    nodes.push(<span key={key++}>{normalized.slice(last)}</span>);
  }
  return <div className="whitespace-pre-wrap break-words">{nodes}</div>;
}

export type MenuKey = "workbench" | "mistakes" | "notes" | "settings";

interface ChatSceneProps {
  messages: ChatMessage[];
  options: OptionCard[];
  mistakes: Mistake[];
  notes: SessionNotes;
  settings: SessionSettings;
  thinking: boolean;
  input: string;
  onInput: (value: string) => void;
  onSend: (text?: string) => void;
  onLogoClick: () => void;
  onSettingsChange: (settings: SessionSettings) => void;
}

/** 场景 B · 对话：纯色背景，功能菜单（学习工作台/错题集合/学习笔记/设置）。 */
export function ChatScene(props: ChatSceneProps) {
  const [menu, setMenu] = useState<MenuKey>("workbench");

  const MENU: { key: MenuKey; label: string }[] = [
    { key: "workbench", label: "学习工作台" },
    { key: "mistakes", label: "错题集合" },
    { key: "notes", label: "学习笔记" },
    { key: "settings", label: "设置" },
  ];

  return (
    <div className="scene absolute inset-0 flex flex-col bg-paper">
      <header className="border-b border-line bg-white/80">
        <div className="mx-auto flex max-w-5xl items-center gap-2 px-6 py-3">
          <button
            onClick={props.onLogoClick}
            className="mr-4 font-display text-xl tracking-tight text-ink transition-opacity hover:opacity-70"
            title="回到问候页"
          >
            学习教练
          </button>
          {MENU.map((item) => (
            <button
              key={item.key}
              onClick={() => setMenu(item.key)}
              className={`rounded-md px-3 py-1.5 text-sm transition-colors ${
                menu === item.key ? "bg-sage text-moss" : "text-muted hover:text-ink"
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </header>

      {menu === "workbench" && <Workbench {...props} />}
      {menu === "mistakes" && <MistakesPanel mistakes={props.mistakes} />}
      {menu === "notes" && <NotesPanel notes={props.notes} />}
      {menu === "settings" && (
        <SettingsPanel
          settings={props.settings}
          history={props.messages}
          onChange={props.onSettingsChange}
        />
      )}
    </div>
  );
}

function Workbench({ messages, options, thinking, input, onInput, onSend }: ChatSceneProps) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-4 overflow-y-auto px-6 py-6">
        {messages.map((turn, index) =>
          // 回答还没写出字时不画空对话框，下面的「我先想想…」先顶着。
          turn.role === "assistant" && !turn.content ? null : (
            <div key={index} className={`flex ${turn.role === "user" ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[80%] rounded-xl px-4 py-2.5 leading-relaxed ${
                  turn.role === "user"
                    ? "rounded-br-md bg-gradient-to-br from-moss to-moss-deep text-white"
                    : "rounded-bl-md border border-line bg-white text-ink"
                }`}
              >
                <MessageContent text={turn.content} />
              </div>
            </div>
          ),
        )}
        {thinking && (
          <div className="flex justify-start" aria-live="polite">
            <div className="rounded-bl-md rounded-xl border border-line bg-white px-4 py-2.5 text-muted">
              我先想想…
            </div>
          </div>
        )}
        {!thinking && options.length > 0 && (
          <div className="grid gap-3 sm:grid-cols-2">
            {options.map((option) => (
              <button
                key={`${option.key}-${option.text}`}
                onClick={() => onSend(option.text)}
                className="group flex items-center gap-3 rounded-xl border border-line bg-white px-4 py-3 text-left shadow-sm transition-all hover:-translate-y-0.5 hover:border-moss/50 hover:shadow-md"
              >
                {option.key && (
                  <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-sage font-display text-sm text-moss">
                    {option.key}
                  </span>
                )}
                <span className="text-sm leading-relaxed text-ink">{option.text}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="mx-auto w-full max-w-3xl px-6 pb-6">
        <div className="flex gap-3">
          <input
            value={input}
            onChange={(event) => onInput(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && onSend()}
            placeholder="问点什么，比如：帮我生成一个对比 1/2 和 1/4 的互动图"
            className="flex-1 rounded-full border border-control bg-white px-6 py-3.5 shadow-sm focus:border-focus focus:outline-none focus:ring-2 focus:ring-focus/20"
          />
          <button
            onClick={() => onSend()}
            disabled={thinking}
            className="rounded-full bg-moss px-8 py-3.5 font-semibold text-white transition-transform hover:scale-[1.03] disabled:opacity-50"
          >
            发送
          </button>
        </div>
      </div>
    </div>
  );
}

function EmptyHint({ text }: { text: string }) {
  return <p className="px-6 py-10 text-center text-muted">{text}</p>;
}

function MistakesPanel({ mistakes }: { mistakes: Mistake[] }) {
  if (!mistakes.length) return <EmptyHint text="还没有错题。做完练习后，答错的题会带着错因出现在这里。" />;
  return (
    <div className="mx-auto w-full max-w-3xl overflow-y-auto px-6 py-6">
      <h2 className="font-display text-2xl text-ink">错题集合</h2>
      <div className="mt-4 flex flex-col gap-3">
        {mistakes.map((mistake, index) => (
          <div key={index} className="rounded-xl border border-line bg-white p-4">
            <div className="flex items-baseline justify-between">
              <span className="text-sm text-moss">{mistake.concept}</span>
              <span className="text-xs text-muted">{mistake.category}</span>
            </div>
            <p className="mt-2 text-sm text-ink">{mistake.question}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function NotesPanel({ notes }: { notes: SessionNotes }) {
  const empty = !notes.profile.length && !notes.concepts.length && !notes.errors.length;
  if (empty) return <EmptyHint text="还没有笔记。一组题做完后选「整理」，笔记会出现在这里（只本页有效）。" />;
  return (
    <div className="mx-auto w-full max-w-3xl overflow-y-auto px-6 py-6">
      <h2 className="font-display text-2xl text-ink">学习笔记</h2>
      {notes.concepts.map((concept, index) => (
        <div key={index} className="mt-4 rounded-xl border border-line bg-white p-4">
          <div className="flex items-baseline justify-between">
            <span className="font-semibold text-ink">{concept.label}</span>
            <span className="text-xs text-muted">{concept.status}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function SettingsPanel({
  settings,
  history,
  onChange,
}: {
  settings: SessionSettings;
  history: ChatMessage[];
  onChange: (settings: SessionSettings) => void;
}) {
  return (
    <div className="mx-auto w-full max-w-3xl overflow-y-auto px-6 py-6">
      <h2 className="font-display text-2xl text-ink">设置</h2>
      <p className="mt-1 text-sm text-muted">开场问过的几件事，随时可以在这里改（只本次会话有效）。</p>

      <div className="mt-5 flex flex-col gap-4">
        <label className="block">
          <span className="text-sm font-semibold text-ink">这次是谁在学</span>
          <select
            value={settings.who}
            onChange={(event) => onChange({ ...settings, who: event.target.value })}
            className="mt-2 w-full rounded-lg border border-control bg-white px-4 py-3 focus:border-focus focus:outline-none focus:ring-2 focus:ring-focus/20"
          >
            {WHO_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>

        <label className="block">
          <span className="text-sm font-semibold text-ink">现在在哪个阶段</span>
          <select
            value={settings.stage}
            onChange={(event) => onChange({ ...settings, stage: event.target.value })}
            className="mt-2 w-full rounded-lg border border-control bg-white px-4 py-3 focus:border-focus focus:outline-none focus:ring-2 focus:ring-focus/20"
          >
            {["未设置", ...STAGE_OPTIONS].map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>

        <label className="flex items-center justify-between rounded-lg border border-line bg-white px-4 py-3">
          <span className="text-sm font-semibold text-ink">保存学习进度</span>
          <span className="flex items-center gap-2">
            <span className="text-xs text-muted">{settings.recordsOn ? "已开启" : "未开启"}</span>
            <button
              role="switch"
              aria-checked={settings.recordsOn}
              onClick={() => onChange({ ...settings, recordsOn: !settings.recordsOn })}
              className={`relative h-6 w-11 rounded-full transition-colors ${
                settings.recordsOn ? "bg-moss" : "bg-control"
              }`}
            >
              <span
                className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all ${
                  settings.recordsOn ? "left-[22px]" : "left-0.5"
                }`}
              />
            </button>
          </span>
        </label>
      </div>

      <h3 className="mt-8 text-lg font-semibold text-ink">历史对话记录（本次会话）</h3>
      {!history.length ? (
        <EmptyHint text="本次会话还没有对话。" />
      ) : (
        <div className="mt-3 flex flex-col gap-2">
          {history.map((turn, index) => (
            <div key={index} className="rounded-lg border border-line bg-white px-4 py-2 text-sm">
              <span className="mr-2 text-xs text-muted">{turn.role === "user" ? "你" : "教练"}</span>
              <span className="text-ink">{turn.content}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
