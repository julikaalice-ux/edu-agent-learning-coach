"""Compare the multi-agent coach with the single-agent baseline on evals/cases.jsonl.

Both run in this process against the real models, with the same model
deployment, the same search provider and the same program checks. For every
case the script plays the learner turns, then measures:

- auto checks: the same checks as evals/score.py (format, intent, one
  question per turn, links only from retrieved sources, replay);
- judge: a separate model call grades the whole conversation against the
  case's "must" and "fail" lists, without knowing which version produced it;
- cost: model calls, input/output tokens and time per learner turn.

Usage (from the repository root, signed in with `az login`):

  uv run --project agent --locked python evals/compare.py --sample 10   # a first small run
  uv run --project agent --locked python evals/compare.py               # all 49 cases

Results go to runs/compare-<time>/: report.md, report.html, summary.json and
the raw rows (responses-*.jsonl, turns-*.jsonl, judge-*.jsonl). Learner and
coach text is kept in these local files only; nothing is uploaded.
"""

import argparse
import asyncio
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
import html
import importlib.util
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
from time import perf_counter
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

_spec = importlib.util.spec_from_file_location("evals_score", Path(__file__).with_name("score.py"))
score = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(score)

ARCHITECTURES = ("multi", "single")
LABELS = {"multi": "多 agent", "single": "单 agent"}
FALLBACK_TEXT = "这次处理没有顺利完成"
# Placeholders in cases.jsonl that the driver fills in for the learner.
FOLLOWUP_ANSWER = "下周三考试，每天晚上能学一小时，函数部分基础一般。"
OPENING_TOPIC = "给我讲讲勾股定理"
# Unscored turns that set the scene a case's driver note asks for.
SETUP_TURNS = {"BEH-E-STOP": ["孩子四岁，我们一起玩一个认识颜色的小游戏吧"]}
JUDGE_PROMPT = """你是学习教练的评测员。input 里有一段对话：learner 是学习者说的话，coach 是学习者实际看到的教练回复。
请按 must（必须做到）和 fail（不能出现）逐条判断，只看对话内容，不猜测系统内部，也不考虑回复是由什么系统生成的。
- must 每条：整段对话里教练做到了就 met=true。
- fail 每条：对话里出现了就 hit=true。
- answer_leaked：教练出了题，并在学习者作答之前，直接或间接透露了正确答案时为 true；没有出题时为 false。判定边界：题目连同选项正常展示（对话里选择题的那一段）不算泄露；只有正确答案出现在教练的正文讲解里、或教练写明了答案的计算过程才算 true。driver 注明是重放（replay）的轮次，重复显示同一条已出过的题不算泄露。
每条 why 用一句中文说明依据。只输出符合 output_schema 的 JSON。"""


def judge_schema(must, fail):
    item = lambda flag: {"type": "object", "properties": {"item": {"type": "string"}, flag: {"type": "boolean"},
                                                           "why": {"type": "string"}},
                         "required": ["item", flag, "why"], "additionalProperties": False}
    return {"type": "object", "properties": {
        "must": {"type": "array", "items": item("met"), "minItems": len(must), "maxItems": len(must)},
        "fail": {"type": "array", "items": item("hit"), "minItems": len(fail), "maxItems": len(fail)},
        "answer_leaked": {"type": "boolean"}},
        "required": ["must", "fail", "answer_leaked"], "additionalProperties": False}


class SwitchProvider:
    """The real search provider, or a fixture a case asks for; records every URL returned."""

    def __init__(self, real):
        self.real, self.fixture, self.urls = real, None, []

    async def search(self, query):
        found = self.fixture if self.fixture is not None else await self.real.search(query)
        found = deepcopy(found)
        self.urls += [item.get("url") for item in found if isinstance(item, dict) and item.get("url")]
        return found


FIXTURES = {
    "RES-FAIL-HONEST": [],
    "VIS-DISPUTED": [
        {"title": "某疗法随机对照试验", "url": "https://trial.example/therapy",
         "excerpt": "一项随机对照试验报告，某疗法组的症状改善优于对照组。", "excerpt_kind": "page_extract"},
        {"title": "某疗法系统综述", "url": "https://review.test/therapy",
         "excerpt": "系统综述认为，某疗法与安慰剂相比没有显著差异，现有试验质量较低。", "excerpt_kind": "page_extract"},
    ],
}


def visible(output):
    text = output["message"]
    if output.get("next_step"):
        text += "\n\n" + output["next_step"]["text"]
    return text


def answer_for(state, wrong=False):
    question = (state.get("quiz") or {}).get("awaiting")
    if not question:
        return None
    index = question["answer_index"]
    if wrong:
        index = next(i for i in range(len(question["choices"])) if i != index)
    return "ABCD"[index]


def sample_cases(cases, size):
    """An even spread across layers, in file order."""
    by_layer = defaultdict(list)
    for case in cases:
        by_layer[case["layer"]].append(case)
    picked, layers = [], list(by_layer)
    while len(picked) < min(size, len(cases)):
        for layer in layers:
            if by_layer[layer] and len(picked) < size:
                picked.append(by_layer[layer].pop(0))
    order = {case["id"]: index for index, case in enumerate(cases)}
    return sorted(picked, key=lambda case: order[case["id"]])


def percentile(values, share):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))]


class Recorder:
    """Collects the per-call records the runners report through the stage observer."""

    def __init__(self):
        self.records = []

    def __call__(self, record):
        self.records.append(dict(record))

    def take(self):
        taken, self.records = self.records, []
        return taken


async def play_case(pipeline, provider, recorder, case):
    """Play one case's learner turns; return one row per learner turn."""
    provider.fixture = FIXTURES.get(case["id"])
    separate = "独立会话" in case.get("driver", "")
    session_id, history, state, rows = f"eval-{uuid.uuid4().hex[:12]}", [], {}, []
    first_turn_messages = None
    turns = [(text, False) for text in SETUP_TURNS.get(case["id"], [])] + [(text, True) for text in case["turns"]]
    index = -1
    for text, scored in turns:
        if separate:
            session_id, history, state = f"eval-{uuid.uuid4().hex[:12]}", [], {}
        if scored:
            index += 1
        if text.startswith("先任选"):
            text = OPENING_TOPIC
        elif text == "{{answer_current_question_correctly}}":
            text = answer_for(state) or "我不知道"
        elif text == "{{answer_current_question_wrong}}":
            text = answer_for(state, wrong=True) or "我不知道"
        elif text == "{{answer_followups_until_plan}}":
            text = FOLLOWUP_ANSWER
        replay = text == "{{replay_turn_0}}"
        sent = deepcopy(first_turn_messages) if replay else history + [{"role": "user", "text": text}]
        provider.urls = []
        recorder.take()
        started = perf_counter()
        error = None
        try:
            output, state = await pipeline.run(sent, session_id=session_id, state=state)
        except Exception as caught:  # a crash is a result too; keep going
            import traceback
            print(f"TRACEBACK {case['id']} turn {index}:", flush=True)
            traceback.print_exc()
            error = type(caught).__name__
            output = {"schema_version": "1.0", "intent": {"primary": "G", "is_implicit": False},
                      "message": FALLBACK_TEXT, "next_step": None}
        elapsed = round((perf_counter() - started) * 1000)
        records = recorder.take()
        if first_turn_messages is None and scored:
            first_turn_messages = deepcopy(sent)
        if not replay:
            history = sent + [{"role": "assistant", "text": visible(output)}]
        rows.append({
            "case_id": case["id"], "turn": index, "scored": scored, "learner": sent[-1]["text"],
            "output": output, "visible": visible(output), "error": error,
            "fallback": FALLBACK_TEXT in output["message"], "elapsed_ms": elapsed,
            "model_calls": sum(record.get("model_calls", 1) for record in records),
            "input_tokens": sum(record.get("input_token_count") or record.get("estimated_input_tokens") or 0
                                for record in records),
            "output_tokens": sum(record.get("output_token_count") or 0 for record in records),
            "roles": [record.get("role") for record in records],
            "allowed_urls": sorted(set(provider.urls)),
            "execution": (state or {}).get("last_execution"),
        })
    return rows


async def judge_case(judge, case, rows):
    must, fail = case["expect"].get("must", []), case["expect"].get("fail", [])
    if not must and not fail:
        return None
    conversation = []
    for row in rows:
        conversation += [{"speaker": "learner", "text": row["learner"]}, {"speaker": "coach", "text": row["visible"]}]
    payload = {"case": case["title"], "note": case.get("driver", ""), "conversation": conversation,
               "must": must, "fail": fail}
    result = await judge(payload, judge_schema(must, fail))
    met = [item["met"] for item in result["must"]]
    hit = [item["hit"] for item in result["fail"]]
    return {"case_id": case["id"], "passed": all(met) and not any(hit) and not result["answer_leaked"],
            "must_met": sum(met), "must_total": len(met), "fail_hits": sum(hit),
            "answer_leaked": result["answer_leaked"], "detail": result}


def make_judge(model):
    from agent_framework import Agent
    from agent_framework.foundry import FoundryChatClient
    from azure.identity import DefaultAzureCredential
    from learning_coach.domain.contracts import ContractError, parse

    client = FoundryChatClient(project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], model=model,
                               credential=DefaultAzureCredential())
    agent = Agent(name="judge", client=client, instructions=JUDGE_PROMPT, default_options={"store": False})

    async def judge(payload, schema):
        request = {"output_schema": schema, "input": payload}
        for _ in range(2):
            response = await agent.run(json.dumps(request, ensure_ascii=False))
            try:
                return parse(response.text, schema)
            except ContractError:
                request["format_correction"] = "上次输出未通过结构校验。只输出符合 output_schema 的 JSON。"
        raise RuntimeError("judge_output_invalid")

    return judge


def auto_check(cases, rows):
    """Run evals/score.py's checks on the scored turns."""
    schema = json.loads((ROOT / "agent/response.schema.json").read_text(encoding="utf-8"))
    validator = score.Draft202012Validator(schema)
    first, results = {}, []
    for row in rows:
        if not row["scored"]:
            continue
        case = cases[row["case_id"]]
        wanted = case["expect"].get("score_turns")
        if wanted is not None and row["turn"] not in wanted:
            continue
        text = json.dumps(row["output"], ensure_ascii=False)
        first.setdefault(row["case_id"], json.dumps(row["output"], ensure_ascii=False, sort_keys=True))
        value, parse_codes = score.parse_strict(text)
        codes = [code for code in parse_codes if "strict_json" in case["expect"].get("auto", [])]
        codes += score.run_checks(case, row["turn"], value, validator, first[row["case_id"]], row["allowed_urls"])
        codes = [code for code in codes if code != "manual_check_required"]
        intent = score.expected_intent(case, row["turn"]) if "intent" in case["expect"].get("auto", []) else None
        results.append({"case_id": row["case_id"], "turn": row["turn"], "layer": case["layer"],
                        "passed": not codes, "codes": codes, "intent_checked": intent is not None,
                        "intent_ok": intent is not None and not any(code.startswith("intent_") for code in codes)})
    return results


def summarize(rows, checks, judged):
    learner_turns = [row for row in rows]
    calls = [row["model_calls"] for row in learner_turns]
    elapsed = [row["elapsed_ms"] for row in learner_turns]
    intents = [item for item in checks if item["intent_checked"]]
    graded = [item for item in judged if item is not None]
    return {
        "cases": len({row["case_id"] for row in rows}), "learner_turns": len(learner_turns),
        "auto_pass_rate": sum(item["passed"] for item in checks) / len(checks) if checks else None,
        "auto_rows": len(checks),
        "intent_accuracy": sum(item["intent_ok"] for item in intents) / len(intents) if intents else None,
        "judge_pass_rate": sum(item["passed"] for item in graded) / len(graded) if graded else None,
        "judged_cases": len(graded),
        "must_met_rate": (sum(item["must_met"] for item in graded) / sum(item["must_total"] for item in graded)
                          if graded and sum(item["must_total"] for item in graded) else None),
        "fail_hits": sum(item["fail_hits"] for item in graded),
        "answer_leaks": sum(item["answer_leaked"] for item in graded),
        "fallback_turns": sum(row["fallback"] for row in learner_turns),
        "errors": sum(row["error"] is not None for row in learner_turns),
        "calls_per_turn": statistics.mean(calls) if calls else None,
        "calls_p90": percentile(calls, .9),
        "input_tokens_per_turn": statistics.mean(row["input_tokens"] for row in learner_turns) if rows else None,
        "output_tokens_per_turn": statistics.mean(row["output_tokens"] for row in learner_turns) if rows else None,
        "total_input_tokens": sum(row["input_tokens"] for row in learner_turns),
        "total_output_tokens": sum(row["output_tokens"] for row in learner_turns),
        "seconds_p50": percentile(elapsed, .5) / 1000 if elapsed else None,
        "seconds_p90": percentile(elapsed, .9) / 1000 if elapsed else None,
    }


METRICS = (
    ("judge_pass_rate", "判分模型：整条用例通过率", "pct", True),
    ("must_met_rate", "判分模型：“必须做到”达成率", "pct", True),
    ("auto_pass_rate", "程序检查通过率", "pct", True),
    ("intent_accuracy", "意图分类准确率", "pct", True),
    ("fail_hits", "“不能出现”被命中次数", "int", False),
    ("answer_leaks", "作答前漏答案次数", "int", False),
    ("fallback_turns", "兜底回复次数（这一轮没做完）", "int", False),
    ("calls_per_turn", "每轮模型调用次数（平均）", "num", False),
    ("calls_p90", "每轮模型调用次数（最多的 10%）", "num", False),
    ("input_tokens_per_turn", "每轮输入 token（平均）", "int", False),
    ("output_tokens_per_turn", "每轮输出 token（平均）", "int", False),
    ("seconds_p50", "每轮耗时，中位数（秒）", "num", False),
    ("seconds_p90", "每轮耗时，最慢的 10%（秒）", "num", False),
)


def fmt(value, kind):
    if value is None:
        return "—"
    if kind == "pct":
        return f"{value * 100:.0f}%"
    if kind == "int":
        return f"{value:.0f}"
    return f"{value:.1f}"


def write_reports(out, summary, per_case, meta):
    archs = [arch for arch in ARCHITECTURES if arch in summary]
    lines = ["# 单 agent 与多 agent 对比", "",
             f"- 时间：{meta['time']}　用例：{meta['cases']} 条　模型：{meta['models']}",
             f"- 判分模型：{meta['judge_model'] or '未运行'}", "",
             "| 指标 | " + " | ".join(LABELS[arch] for arch in archs) + " |",
             "|---|" + "---|" * len(archs)]
    for key, label, kind, _ in METRICS:
        lines.append(f"| {label} | " + " | ".join(fmt(summary[arch][key], kind) for arch in archs) + " |")
    lines += ["", "## 每条用例", "",
              "| 用例 | 层 | " + " | ".join(f"{LABELS[arch]}：判分 / 调用次数" for arch in archs) + " |",
              "|---|---|" + "---|" * len(archs)]
    for case_id, item in per_case.items():
        cells = []
        for arch in archs:
            data = item.get(arch) or {}
            verdict = {True: "通过", False: "不通过", None: "—"}[data.get("judge")]
            cells.append(f"{verdict} / {data.get('calls', '—')}")
        lines.append(f"| {case_id} | {item['layer']} | " + " | ".join(cells) + " |")
    lines += ["", "说明：判分模型只看学习者看到的对话，不知道是哪一版生成的；程序检查与 evals/score.py 相同。"
              "两版用同一个模型部署、同一个搜索服务和同样的程序检查。"]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows = []
    for key, label, kind, higher in METRICS:
        values = [summary[arch][key] for arch in archs]
        top = max((value for value in values if value is not None), default=0) or 1
        bars = "".join(
            f'<div class="bar {arch}"><span class="name">{LABELS[arch]}</span>'
            f'<span class="track"><span class="fill" style="width:{(0 if value is None else value / top) * 100:.0f}%"></span></span>'
            f'<span class="value">{fmt(value, kind)}</span></div>' for arch, value in zip(archs, values))
        hint = "越高越好" if higher else "越低越好"
        rows.append(f'<section><h3>{html.escape(label)}<small>{hint}</small></h3>{bars}</section>')
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>单 agent 与多 agent 对比</title>
<style>
:root{{--bg:#fff;--ink:#1f2933;--muted:#667085;--line:#e4e7ec;--multi:#3f6fc4;--single:#c98a2b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111418;--ink:#e6e9ee;--muted:#98a2b3;--line:#2a3038}}}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,sans-serif}}
main{{max-width:760px;margin:0 auto}} h1{{font-size:22px;margin:0 0 4px}} p{{color:var(--muted);margin:0 0 20px}}
section{{border-top:1px solid var(--line);padding:12px 0}} h3{{font-size:15px;margin:0 0 8px;display:flex;gap:8px;align-items:baseline}}
small{{color:var(--muted);font-weight:400}} .bar{{display:grid;grid-template-columns:72px 1fr 64px;gap:10px;align-items:center;margin:4px 0}}
.track{{background:var(--line);border-radius:4px;height:12px;overflow:hidden}} .fill{{display:block;height:100%}}
.multi .fill{{background:var(--multi)}} .single .fill{{background:var(--single)}} .value{{text-align:right;font-variant-numeric:tabular-nums}}
</style></head><body><main><h1>单 agent 与多 agent 对比</h1>
<p>{html.escape(meta['time'])} · {meta['cases']} 条用例 · 模型 {html.escape(meta['models'])} · 判分模型 {html.escape(meta['judge_model'] or '未运行')}</p>
{''.join(rows)}</main></body></html>"""
    (out / "report.html").write_text(page, encoding="utf-8")


def configure_environment(production_models):
    """Same deployment for every role unless asked otherwise; no side effects outside this run."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / "agent/.env")
    os.environ["LEARNING_COACH_ONBOARDING"] = "0"
    os.environ["LEARNING_COACH_DEMO_MODE"] = "0"
    os.environ["LEARNING_COACH_MEMORY_STORE"] = ""        # never write eval traffic to Foundry Memory
    os.environ["LEARNING_COACH_ARTIFACT_BLOB_URL"] = ""   # visuals stay in the temporary folder
    model = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    if not production_models:
        # Set, not delete: the pipeline loads agent/.env again and would restore deleted names.
        from learning_coach.adapters.runner import ROLES
        for role in (*ROLES, "single"):
            os.environ[f"LEARNING_COACH_MODEL_{role.upper()}"] = model
    return model


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arch", default="multi,single", help="multi、single，或两者（逗号分隔）")
    parser.add_argument("--sample", type=int, default=0, help="只跑这么多条，按层均匀挑选；0 表示全部")
    parser.add_argument("--cases", default="", help="只跑这些用例 id（逗号分隔）")
    parser.add_argument("--judge-model", default="", help="判分模型部署名，默认与被测模型相同")
    parser.add_argument("--no-judge", action="store_true", help="不跑判分模型，只做程序检查和成本统计")
    parser.add_argument("--production-models", action="store_true",
                        help="多 agent 按 LEARNING_COACH_MODEL_<角色> 的分工跑（默认所有角色同一个模型）")
    parser.add_argument("--out", default="", help="结果目录，默认 runs/compare-<时间>")
    args = parser.parse_args()

    archs = [arch.strip() for arch in args.arch.split(",") if arch.strip()]
    if not archs or any(arch not in ARCHITECTURES for arch in archs):
        parser.error("--arch 只能是 multi、single")
    model = configure_environment(args.production_models)
    cases = list(score.load_cases(Path(__file__).with_name("cases.jsonl")).values())
    if args.cases:
        wanted = {item.strip() for item in args.cases.split(",")}
        cases = [case for case in cases if case["id"] in wanted]
    elif args.sample:
        cases = sample_cases(cases, args.sample)
    by_id = {case["id"]: case for case in cases}
    out = Path(args.out or ROOT / "runs" / f"compare-{datetime.now():%Y%m%d-%H%M}")
    out.mkdir(parents=True, exist_ok=True)
    judge = None if args.no_judge else make_judge(args.judge_model or model)

    from learning_coach.adapters.search_providers import default_provider
    from learning_coach.server import create_pipeline

    summary, per_case = {}, {case["id"]: {"layer": case["layer"]} for case in cases}
    for arch in archs:
        recorder, provider = Recorder(), SwitchProvider(default_provider())
        with tempfile.TemporaryDirectory() as folder:
            pipeline = create_pipeline(data_dir=folder, architecture=arch, search_provider=provider,
                                       stage_observer=recorder)
            rows, judged = [], []
            for number, case in enumerate(cases, 1):
                print(f"[{LABELS[arch]}] {number}/{len(cases)} {case['id']}", flush=True)
                case_rows = await play_case(pipeline, provider, recorder, case)
                rows += case_rows
                verdict = None
                if judge is not None:
                    try:
                        verdict = await judge_case(judge, case, case_rows)
                    except Exception as error:
                        print(f"  判分失败：{type(error).__name__}", flush=True)
                judged.append(verdict)
                per_case[case["id"]][arch] = {
                    "judge": None if verdict is None else verdict["passed"],
                    "calls": sum(row["model_calls"] for row in case_rows)}
            pipeline.store.close()
        checks = auto_check(by_id, rows)
        summary[arch] = summarize(rows, checks, judged)
        with open(out / f"turns-{arch}.jsonl", "w", encoding="utf-8") as handle:
            handle.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        with open(out / f"responses-{arch}.jsonl", "w", encoding="utf-8") as handle:
            handle.writelines(json.dumps({"case_id": row["case_id"], "turn": row["turn"],
                                          "text": json.dumps(row["output"], ensure_ascii=False),
                                          "allowed_urls": row["allowed_urls"]}, ensure_ascii=False) + "\n"
                              for row in rows if row["scored"])
        with open(out / f"judge-{arch}.jsonl", "w", encoding="utf-8") as handle:
            handle.writelines(json.dumps(item, ensure_ascii=False) + "\n" for item in judged if item)
        with open(out / f"checks-{arch}.jsonl", "w", encoding="utf-8") as handle:
            handle.writelines(json.dumps(item, ensure_ascii=False) + "\n" for item in checks)

    meta = {"time": f"{datetime.now():%Y-%m-%d %H:%M}", "cases": len(cases),
            "models": "按角色分工" if args.production_models else model,
            "judge_model": None if args.no_judge else (args.judge_model or model)}
    (out / "summary.json").write_text(json.dumps({"meta": meta, "summary": summary, "per_case": per_case},
                                                 ensure_ascii=False, indent=2), encoding="utf-8")
    write_reports(out, summary, per_case, meta)
    print((out / "report.md").read_text(encoding="utf-8"))
    print(f"结果在 {out}")


if __name__ == "__main__":
    asyncio.run(main())
