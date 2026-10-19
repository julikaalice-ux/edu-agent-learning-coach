"""Measure visual-generation success rate, stability and timing on evals/visual_cases.jsonl.

For every case the script plays the same learner prompt RUNS times (fresh
session each run, real models, demo mode on so onboarding is skipped) and
records:

- success: the reply carries an 打开互动图/看题目配图 link AND the artifact
  is retrievable from the store (published, not just prepared);
- drafts: how many drafts the visual pipeline needed (1 or 2), as the
  pipeline reports it, plus the failure code when no visual was published;
- timing: whole-turn elapsed plus each visual stage's share (designer,
  reviewer, picture generation, web picture search) and the model tokens;
- stability: the per-case success pattern across runs (3/3 vs 1/3).

Usage (from the repository root, signed in with `az login`):

  uv run --project agent --locked python evals/visual_quality.py             # 3 runs per case
  uv run --project agent --locked python evals/visual_quality.py --runs 1    # quick smoke

Results go to runs/visual-<time>/: rows.jsonl and report.md. Learner and
coach text stays in those local files only; nothing is uploaded.
"""

import argparse
import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
from statistics import mean
import sys
from time import perf_counter
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

LINK = re.compile(r"\[(?:打开互动图|看题目配图)\]\([^)]*?/artifacts/([A-Za-z0-9_-]+)(?:\.html)?\)")
FALLBACK_TEXT = "这次处理没有顺利完成"
VISUAL_ROLES = {"visualizer", "verifier"}
STAGES = {"visualizer": "画图", "verifier": "审图", "image": "生成图片", "image_search": "找网络图片"}
# Everything else in the turn, so time outside the visual can be explained too.
OTHER_STAGES = {("coordinator", "decide"): "理解问题", ("coordinator", "respond"): "写回复",
                ("quizzer", "quiz"): "出题"}


class ThrottleWatch(logging.Handler):
    """Counts the model SDK's silent waits: HTTP statuses (e.g. 429 = over the
    deployment's per-minute limit) and "Retrying request in N seconds".
    Keeps only status codes and wait seconds, never URLs or content."""

    STATUS = re.compile(r'HTTP/[\d.]+ (\d{3})')
    RETRY = re.compile(r"Retrying request in ([\d.]+) seconds")

    def __init__(self):
        super().__init__(logging.INFO)
        self.take()

    def emit(self, record):
        message = record.getMessage()
        status = self.STATUS.search(message) if record.name.startswith("httpx") else None
        if status and status.group(1) != "200":
            self.statuses[status.group(1)] = self.statuses.get(status.group(1), 0) + 1
        retry = self.RETRY.search(message)
        if retry:
            self.retries += 1
            self.wait += float(retry.group(1))

    def take(self):
        taken = {"retries": getattr(self, "retries", 0), "wait_s": round(getattr(self, "wait", 0.0), 1),
                 "statuses": dict(getattr(self, "statuses", {}))}
        self.retries, self.wait, self.statuses = 0, 0.0, {}
        return taken

    def install(self):
        for name in ("openai._base_client", "httpx"):
            logger = logging.getLogger(name)
            logger.setLevel(logging.INFO)
            logger.addHandler(self)
        return self


WATCH = ThrottleWatch()


def percentile(values, share):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))]


def load_cases(path):
    cases = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    for case in cases:
        for field in ("id", "prompt"):
            if field not in case:
                raise ValueError(f"visual_cases: missing {field}")
    return cases


def collect(records):
    """Per-call records -> (visual_ms, per-stage ms, visual_in, visual_out).

    Picture search runs per panel in parallel, so its share is the longest
    single search, not the sum.
    """
    visual = [record for record in records if record.get("role") in VISUAL_ROLES]
    stages = {name: sum(record.get("elapsed_ms", 0) for record in records if record.get("role") == role)
              for role, name in STAGES.items() if role != "image_search"}
    stages["找网络图片"] = max((record.get("elapsed_ms", 0) for record in records
                              if record.get("role") == "image_search"), default=0)
    return (
        sum(stages.values()),
        stages,
        sum(record.get("input_token_count") or record.get("estimated_input_tokens") or 0
            for record in visual),
        sum(record.get("output_token_count") or 0 for record in visual),
    )


# What a learner who tapped "允许本次外部访问" grants: the eval measures the
# visuals, not the consent question (--no-external measures the local path).
EXTERNAL_GRANTS = frozenset({"research", "image_search", "image_generation"})


async def run_case(pipeline, recorder, case, runs, grants=EXTERNAL_GRANTS):
    rows = []
    for run in range(1, runs + 1):
        recorder.take()
        WATCH.take()
        session_id = f"visual-{uuid.uuid4().hex[:12]}"
        started = perf_counter()
        error, state = None, {}
        try:
            output, state = await pipeline.run([{"role": "user", "text": case["prompt"]}],
                                               session_id=session_id, turn_id="1",
                                               external_permissions=grants)
        except Exception as caught:
            error = type(caught).__name__
            output = {"schema_version": "1.0", "intent": {"primary": "G", "is_implicit": False},
                      "message": FALLBACK_TEXT, "next_step": None}
        elapsed_ms = round((perf_counter() - started) * 1000)
        records = recorder.take()
        throttle = WATCH.take()
        visual_ms, stages, visual_in, visual_out = collect(records)
        execution = (state or {}).get("last_execution") or {}
        visual = execution.get("visual") or {}
        attempts = visual.get("drafts") or 0
        token = LINK.search(output.get("message", ""))
        published = False
        if token is not None:
            try:
                published = pipeline.artifacts.get(token.group(1)) is not None
            except Exception:
                published = False
        rows.append({
            "case_id": case["id"], "run": run, "elapsed_ms": elapsed_ms,
            "visual_ms": visual_ms, "attempts": attempts, "stages_ms": stages,
            "visual_kind": visual.get("kind"), "visual_failure": visual.get("failure"),
            "visual_issues": visual.get("issues") or [],
            # Set when the coordinator answered directly and the program had to redo the route.
            "routing_repaired": execution.get("routing_repaired"),
            "visual_input_tokens": visual_in, "visual_output_tokens": visual_out,
            "success": not error and token is not None and published,
            "error": error, "fallback": FALLBACK_TEXT in output["message"],
            "coach_text": output["message"],
        })
        others = {}
        for record in records:
            name = OTHER_STAGES.get((record.get("role"), record.get("phase")))
            if name:
                others[name] = others.get(name, 0) + record.get("elapsed_ms", 0)
        rows[-1]["other_ms"] = others
        # Only codes and timings, never content: enough to see where a slow turn went.
        rows[-1]["calls"] = [{key: record.get(key) for key in ("role", "phase", "attempt", "outcome", "elapsed_ms",
                                                              "input_token_count", "output_token_count",
                                                              "results", "invalid_at", "query", "listed",
                                                              "retried", "downloaded", "paused", "error")
                              if key in record}
                             for record in records]
        failed = [call for call in rows[-1]["calls"] if call.get("outcome") == "provider_or_timeout_failure"]
        # A turn that broke before (or outside) drawing has no visual failure code of its own.
        rows[-1]["turn_failure"] = None if visual.get("failure") or rows[-1]["success"] or not failed else (
            "调用失败：" + "、".join(sorted({OTHER_STAGES.get((call["role"], call["phase"]), call["role"])
                                         for call in failed})))
        if (state or {}).get("pending_external") and not rows[-1]["success"]:
            rows[-1]["turn_failure"] = "停在联网授权：" + "、".join(state["pending_external"]["capabilities"])
        rows[-1]["visual_history"] = visual.get("history") or []
        rows[-1]["throttle"] = throttle
        shares = "，".join(f"{name} {ms / 1000:.1f}s" for name, ms in {**stages, **others}.items() if ms)
        if throttle["retries"] or throttle["statuses"]:
            shares += (f"；模型服务让等了 {throttle['retries']} 次共 {throttle['wait_s']}s"
                       f"（状态码 {throttle['statuses'] or '-'}）")
        if rows[-1]["routing_repaired"]:
            shares += f"；路由被程序改过（{rows[-1]['routing_repaired']}）"
        print(f"{case['id']} run={run}: {'OK' if rows[-1]['success'] else 'FAIL'} "
              f"({elapsed_ms / 1000:.1f}s；{shares or '没有画图'}；{attempts} 稿；"
              f"{visual.get('kind') or '-'}{'，失败：' + visual['failure'] if visual.get('failure') else ''}"
              f"{'，' + rows[-1]['turn_failure'] if rows[-1]['turn_failure'] else ''})",
              flush=True)
        for issue in rows[-1]["visual_history"] or rows[-1]["visual_issues"]:
            print(f"    退回：{issue[:200]}", flush=True)
        for call in rows[-1]["calls"]:
            if call.get("error"):
                stage = OTHER_STAGES.get((call["role"], call["phase"]), call["role"])
                print(f"    {stage}（{call['role']}）第 {call['attempt']} 次调用失败：{call['error']}", flush=True)
            if call.get("invalid_at"):
                print(f"    {call['role']} 输出格式不对：{call['invalid_at']}", flush=True)
            if call.get("role") == "image_search" and not call.get("results"):
                print(f"    有一格没找到网络图片：搜「{call.get('query', '')}」，候选 {call.get('listed', '?')} 个，"
                      f"下载成功 {call.get('downloaded', '?')} 张"
                      + (f"，重试用「{call['retried']}」" if call.get("retried") else "")
                      + (f"，暂停中的来源 {call['paused']}" if call.get("paused") else ""), flush=True)
    return rows


def summarize(rows, runs):
    by_case = {}
    for row in rows:
        by_case.setdefault(row["case_id"], []).append(row)
    lines = []
    totals = {"success": 0, "rows": 0, "elapsed": [], "visual": [], "attempts": []}
    for case_id in sorted(by_case):
        case_rows = by_case[case_id]
        ok = sum(row["success"] for row in case_rows)
        elapsed = [row["elapsed_ms"] for row in case_rows]
        visual = [row["visual_ms"] for row in case_rows]
        attempts = [row["attempts"] for row in case_rows]
        kinds = sorted({row.get("visual_kind") or "-" for row in case_rows})
        failures = sorted({row.get("visual_failure") or row.get("turn_failure")
                           for row in case_rows if row.get("visual_failure") or row.get("turn_failure")})
        lines.append({
            "case_id": case_id,
            "kinds": "、".join(kinds),
            "failures": "、".join(failures) or "-",
            "success": f"{ok}/{runs}",
            "attempts": f"{mean(attempts):.1f}",
            "total_s": f"{percentile(elapsed, .5) / 1000:.0f} / {percentile(elapsed, .9) / 1000:.0f}",
            "visual_s": f"{percentile(visual, .5) / 1000:.0f} / {percentile(visual, .9) / 1000:.0f}",
        })
        totals["success"] += ok
        totals["rows"] += len(case_rows)
        totals["elapsed"] += elapsed
        totals["visual"] += visual
        totals["attempts"] += attempts
    stage_p50 = {}
    for name in STAGES.values():
        spent = [row["stages_ms"].get(name, 0) for row in rows if row.get("stages_ms", {}).get(name)]
        if spent:
            stage_p50[name] = percentile(spent, .5) / 1000
    failure_counts = {}
    for row in rows:
        reason = row.get("visual_failure") or row.get("turn_failure")
        if reason:
            failure_counts[reason] = failure_counts.get(reason, 0) + 1
    return lines, {
        "cases": len(by_case), "runs_per_case": runs,
        "stage_p50_s": stage_p50, "failures": failure_counts,
        "throttle": {"retries": sum(row.get("throttle", {}).get("retries", 0) for row in rows),
                     "wait_s": sum(row.get("throttle", {}).get("wait_s", 0) for row in rows),
                     "statuses": {code: sum(row.get("throttle", {}).get("statuses", {}).get(code, 0) for row in rows)
                                  for code in sorted({code for row in rows
                                                      for code in row.get("throttle", {}).get("statuses", {})})}},
        "success_rate": totals["success"] / totals["rows"] if totals["rows"] else None,
        "stable_cases": sum(1 for row in lines if row["success"] == f"{runs}/{runs}"),
        "flaky_cases": sum(1 for row in lines if row["success"] not in (f"0/{runs}", f"{runs}/{runs}")),
        "zero_cases": sum(1 for row in lines if row["success"] == f"0/{runs}"),
        "total_p50_s": percentile(totals["elapsed"], .5) / 1000 if totals["elapsed"] else None,
        "total_p90_s": percentile(totals["elapsed"], .9) / 1000 if totals["elapsed"] else None,
        "visual_p50_s": percentile(totals["visual"], .5) / 1000 if totals["visual"] else None,
        "visual_p90_s": percentile(totals["visual"], .9) / 1000 if totals["visual"] else None,
        "mean_attempts": mean(totals["attempts"]) if totals["attempts"] else None,
    }


def write_reports(out, summary, lines, meta):
    report = [
        "# 生图质量评测（成功率 · 稳定性 · 耗时）", "",
        f"- 时间：{meta['time']}　用例：{summary['cases']} 条　每用例跑 {summary['runs_per_case']} 次　"
        f"模型：{meta['models']}", "",
        "| 用例 | 成功率 | 图的类型 | 平均稿次 | 总耗时 p50/p90（秒） | 视觉阶段 p50/p90（秒） | 失败原因 |",
        "|---|---|---|---|---|---|---|",
    ]
    for line in lines:
        report.append(f"| {line['case_id']} | {line['success']} | {line['kinds']} | {line['attempts']} | "
                      f"{line['total_s']} | {line['visual_s']} | {line['failures']} |")
    report += ["", "## 汇总", "",
               f"- 整体成功率：**{summary['success_rate'] * 100:.0f}%**"
               f"（{summary['stable_cases']} 条全过，{summary['flaky_cases']} 条时好时坏，"
               f"{summary['zero_cases']} 条全挂）",
               f"- 总耗时：中位 {summary['total_p50_s']:.0f}s，最慢 10% {summary['total_p90_s']:.0f}s",
               f"- 视觉阶段：中位 {summary['visual_p50_s']:.0f}s，最慢 10% {summary['visual_p90_s']:.0f}s",
               f"- 平均稿次：{summary['mean_attempts']:.2f}（1 = 第一稿就定了；2 = 重画过；0 = 这一轮没有画图）",
               "- 各步耗时中位（只算用到这一步的轮次）：" + ("，".join(
                   f"{name} {seconds:.1f}s" for name, seconds in summary["stage_p50_s"].items()) or "无"),
               "- 失败原因：" + ("，".join(f"{code} {count} 次" for code, count in summary["failures"].items())
                                  or "无"),
               f"- 模型服务让等（限流后自动重试）：{summary['throttle']['retries']} 次，共 {summary['throttle']['wait_s']:.0f}s，"
               f"状态码 {summary['throttle']['statuses'] or '无'}",
               "", "说明：success 要求回复带产物链接且产物确实已发布；稿次和失败原因来自流水线自己的记录。"]
    (out / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n".join(report))


async def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=3, help="runs per case (default 3)")
    parser.add_argument("--cases", default=str(Path(__file__).with_name("visual_cases.jsonl")))
    parser.add_argument("--only", default="", help="只跑这些用例，逗号分隔，如 VISQ-CELLS,VISQ-PHOTO")
    parser.add_argument("--no-external", action="store_true",
                        help="不给联网授权（查资料、搜图、AI 生图都不可用），只测本地图解")
    args = parser.parse_args()

    os.environ["LEARNING_COACH_ONBOARDING"] = "0"
    os.environ["LEARNING_COACH_DEMO_MODE"] = "1"
    os.environ["LEARNING_COACH_DEMO_STAGE"] = "高中"
    os.environ["LEARNING_COACH_MEMORY_STORE"] = ""        # never write eval traffic to Foundry Memory
    os.environ["LEARNING_COACH_ARTIFACT_BLOB_URL"] = ""   # visuals stay in the temporary folder
    from learning_coach.server import create_pipeline  # create_pipeline loads agent/.env itself

    class Recorder:
        def __init__(self):
            self.records = []

        def __call__(self, record):
            self.records.append(dict(record))

        def take(self):
            taken, self.records = self.records, []
            return taken

    recorder = Recorder()
    WATCH.install()
    pipeline = create_pipeline(stage_observer=recorder)
    cases = load_cases(args.cases)
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        cases = [case for case in cases if case["id"] in wanted]
    rows = []
    try:
        for case in cases:
            rows += await run_case(pipeline, recorder, case, args.runs,
                                   frozenset() if args.no_external else EXTERNAL_GRANTS)
    finally:
        pipeline.store.close()

    out = ROOT / "runs" / f"visual-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "rows.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                                    encoding="utf-8")
    lines, summary = summarize(rows, args.runs)
    default = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "")
    models = "，".join(f"{role} {os.environ.get('LEARNING_COACH_MODEL_' + role.upper(), '').strip() or default}"
                      for role in ("coordinator", "visualizer", "verifier"))
    write_reports(out, summary, lines, {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "models": models})
    print(f"\n结果在 {out}")


if __name__ == "__main__":
    asyncio.run(main())
