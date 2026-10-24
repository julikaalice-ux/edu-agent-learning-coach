import { useState } from "react";
import type { Mistake, StudyNote, SessionNotes, LearningAction, LearningReply } from "../lib/api";

const button = "rounded-lg border border-line px-3 py-2 text-sm disabled:opacity-40";
type Props = { mode: "mistakes" | "notes"; mistakes: Mistake[]; notes: SessionNotes;
  enabled: boolean; busy: boolean; message: string;
  onLearning: (action: LearningAction) => Promise<LearningReply>; onPractice: (text: string) => void; onShowMistakes: () => void };

function RetryCard({ card, busy, onLearning, onRelated, onPractice }: {
  card: Mistake; busy: boolean; onLearning: Props["onLearning"]; onRelated: () => void; onPractice: () => void;
}) {
  const [choice, setChoice] = useState("");
  const [feedback, setFeedback] = useState<LearningReply["result"]>();
  const [revealed, setRevealed] = useState(false);
  const [eventId, setEventId] = useState(() => crypto.randomUUID());
  const [error, setError] = useState("");
  const [imageFailed, setImageFailed] = useState(false);
  const imageToken = card.image_url?.match(/\/(?:artifacts|artifact)\/([A-Za-z0-9_-]+)(?:\.html)?$/)?.[1];
  async function act(reveal = false) {
    setError("");
    try {
      const result = await onLearning({ action: reveal ? "reveal" : "retry", card_id: card.id, choice, event_id: eventId });
      setFeedback(result.result); if (reveal || result.result?.correct) setRevealed(true);
    } catch (e) { setError(e instanceof Error ? e.message : "提交失败，请重试"); }
  }
  return <article className="rounded-xl border border-line bg-white p-5">
    <div className="flex justify-between gap-2"><h3 className="font-semibold">{card.title}</h3><span className="text-sm text-muted">{card.status} · 重做 {card.attempts} 次</span></div>
    <p className="mt-1 text-xs text-muted">首次答错：{new Date(card.created_at).toLocaleDateString()}</p>
    <p className="my-4 whitespace-pre-wrap">{card.question}</p>
    {imageToken && <img src={`/artifact/${imageToken}/image`} alt="原题配图" onError={() => setImageFailed(true)} className="my-3 max-h-80" />}
    {card.image_url && (!imageToken || imageFailed) && <p role="alert">原题配图暂不可用，请先做变式题，避免缺少题目条件。</p>}
    <fieldset disabled={busy || !!feedback} className="space-y-2"><legend className="sr-only">选择答案</legend>
      {card.choices.map((option, index) => <label key={option.id} className={`flex cursor-pointer gap-3 rounded-lg border p-3 ${choice === option.id ? "border-moss bg-paper" : "border-line"}`}>
        <input type="radio" name={card.id} checked={choice === option.id} onChange={() => setChoice(option.id)} />
        <span>{String.fromCharCode(65 + index)}. {option.text}</span>
      </label>)}
    </fieldset>
    {revealed && <p className="mt-3 text-sm text-muted">已查看答案或解析，后续作答会记录为辅助后完成。</p>}
    {feedback && <div role="status" className="my-3 whitespace-pre-wrap rounded-lg bg-paper p-3">
      <p>{feedback.message || "原题解析"}{feedback.assisted ? "（查看解析后完成）" : ""}</p>
      {feedback.answer !== undefined && <p>正确选项：{String.fromCharCode(65 + Number(feedback.answer))}；上次选择：{String.fromCharCode(65 + Number(feedback.original_choice))}</p>}
      {feedback.category && <p>错因：{{ conceptual: "概念性误解", step_skipping: "步骤性跳跃", misreading: "审题偏差", transfer_failure: "迁移失败", unclassified: "原因待确认" }[feedback.category] || "原因待确认"}（待确认）</p>}
      <p>{feedback.explanation}</p>
    </div>}
    {error && <p role="alert">{error}</p>}
    <div className="mt-4 flex flex-wrap gap-2">
      {!feedback ? <button className={button} disabled={busy || choice === "" || !!card.image_url && (!imageToken || imageFailed)} onClick={() => void act()}>提交答案</button>
        : <button className={button} disabled={busy} onClick={() => { setFeedback(undefined); setChoice(""); setEventId(crypto.randomUUID()); }}>再试一次</button>}
      <button className={button} disabled={busy} onClick={() => void act(true)}>查看解析与上次作答</button>
      <button className={button} disabled={busy} onClick={onPractice}>做一道变式题</button>
      <button className={button} onClick={onRelated}>回顾相关笔记</button>
    </div>
  </article>;
}

function NoteEditor({ note, busy, onLearning }: { note: StudyNote; busy: boolean; onLearning: Props["onLearning"] }) {
  const [editing, setEditing] = useState(false);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const labels: Record<string, string> = { title: "标题", summary: "核心结论", explanation: "为什么", example: "例子", pitfall: "易混点", conditions: "适用条件" };
  if (!editing) return <button className={`${button} ml-2`} disabled={busy} onClick={() => {
    setFields(Object.fromEntries(Object.keys(labels).map(key => [key, String(note[key as keyof StudyNote] ?? "")]))); setEditing(true);
  }}>编辑笔记</button>;
  return <div className="mt-4 space-y-3">{Object.entries(labels).map(([key, label]) => <label className="block" key={key}>{label}
    <textarea className="block w-full rounded border p-2" value={fields[key]} maxLength={key === "title" ? 200 : key === "example" || key === "explanation" ? 4000 : 2000}
      onChange={e => setFields(prev => ({ ...prev, [key]: e.target.value }))} />
  </label>)}{error && <p role="alert">{error}</p>}
    <button className={button} disabled={busy} onClick={async () => {
      try { await onLearning({ action: "edit", card_id: note.id, fields }); setEditing(false); }
      catch { setError("保存失败，请检查必填内容或稍后重试。"); }
    }}>保存修改</button><button className={`${button} ml-2`} disabled={busy} onClick={() => setEditing(false)}>取消</button>
  </div>;
}

export function LearningPanels(props: Props) {
  const [focus, setFocus] = useState<string | null>(null);
  const cards = focus ? props.notes.cards.filter(n => n.concept_id === focus) : props.notes.cards;
  const run = (action: LearningAction) => { void props.onLearning(action).catch(() => {}); };
  return <section className="mx-auto w-full max-w-3xl overflow-y-auto px-6 py-6">
    <h2 className="font-display text-2xl">{props.mode === "mistakes" ? "错题集合" : "学习笔记"}</h2>
    <p className="mt-2 text-sm text-muted">记录仅在当前会话使用。重做正确不等于已经掌握。</p>
    <div className="my-4 flex gap-2 flex-wrap">
      <button className={button} disabled={props.busy} onClick={() => run({ action: "read" })}>刷新记录</button>
      {!props.enabled && <button className={button} disabled={props.busy} onClick={() => run({ action: "records", enabled: true })}>开启学习记录</button>}
      <button className={button} disabled={props.busy || !props.enabled} onClick={() => run({ action: "compile" })}>{props.busy ? "正在处理…" : "整理本次学习笔记"}</button>
    </div>
    {props.message && <p role="status" className="mb-4">{props.message}</p>}
    {!props.enabled ? <p>请先开启记录；答错后会保存原题，笔记需要点击整理。</p> : <>
      {props.mode === "mistakes" && <div className="space-y-4">
        {!props.mistakes.length && <p>还没有保存的错题。开启记录后完成练习，独立答错的题会出现在这里。</p>}
        {props.mistakes.filter(card => !focus || card.concept_id === focus).map(card => <RetryCard key={card.id} card={card} busy={props.busy} onLearning={props.onLearning}
          onRelated={() => setFocus(card.concept_id)} onPractice={() => props.onPractice(`请围绕“${card.title}”出一道新的变式题，一次一道。`)} />)}
      </div>}
      {(props.mode === "notes" || focus) && <div className="mt-4 space-y-4">
        {focus && <button className={button} onClick={() => setFocus(null)}>关闭关联笔记</button>}
        {!cards.length && <p>暂无{focus ? "关联" : "知识"}笔记。完成讲解后，点击“整理本次学习笔记”。</p>}
        {cards.map(note => <article key={note.id} className="rounded-xl border border-line bg-white p-5">
          <h3 className="text-lg font-semibold">{note.title}</h3>
          <p className="text-xs text-muted">{note.source} · {new Date(note.created_at).toLocaleDateString()} · {note.source.includes("编辑") ? "个人编辑内容，不作为掌握证据" : "已讲解，不代表已掌握"}</p>
          {[["核心结论", note.summary], ["为什么", note.explanation], ["看一个例子", note.example], ["容易混淆", note.pitfall], ["适用条件", note.conditions]].map(([title, text]) => text && <div key={title} className="mt-4"><h4 className="font-semibold">{title}</h4><p className="whitespace-pre-wrap">{text}</p></div>)}
          <p className="mt-3 text-sm text-muted">{note.practice ? `已完成 ${note.practice.attempts} 题，答对 ${note.practice.correct} 题。` : "暂无对应练习记录。"}</p>
          <p className="mt-2 text-sm">下一步：试着用自己的话解释核心结论，再完成一道练习。</p>
          <button className={`${button} mt-4`} disabled={props.busy} onClick={() => props.onPractice(`围绕“${note.title}”出一道练习题。`)}>做一道练习</button>
          <button className={`${button} ml-2`} disabled={!props.mistakes.some(card => card.concept_id === note.concept_id)} onClick={() => { setFocus(note.concept_id); props.onShowMistakes(); }}>查看关联错题 · {props.mistakes.filter(card => card.concept_id === note.concept_id).length}</button>
          <NoteEditor note={note} busy={props.busy} onLearning={props.onLearning} />
        </article>)}
      </div>}
    </>}
  </section>;
}
