"""Run the single-turn cases against both hosted agents in Foundry, side by side.

Creates (once) a prompt-based custom evaluator that grades each reply against
the case's "must" and "fail" lists, uploads the single-turn cases as a
dataset, creates one evaluation and starts one run per hosted agent. Both runs
then show up in the Foundry portal under Evaluation, where they can be
compared.

Needs: `az login`, agent/.env with FOUNDRY_PROJECT_ENDPOINT, both hosted
agents deployed (`azd deploy`), and LEARNING_COACH_ONBOARDING=0 on them (set
in azure.yaml), otherwise every reply is the first-meeting question.

  uv run --project agent --locked python evals/foundry_compare.py            # start both runs
  uv run --project agent --locked python evals/foundry_compare.py --wait     # and wait for the results

Only the 31 single-turn cases fit: Foundry sends one message per row, so
multi-turn practice flows stay in evals/compare.py.
"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
EVALUATOR = "edu-agent-must-fail"
MULTI_AGENT = "agent-framework-agent-with-local-tools-responses"
SINGLE_AGENT = "edu-agent-single"
PROMPT = """你是学习教练的评测员。学习者发来一句话，教练给了回复。请按这条用例的要求打分。

学习者：
{{query}}

教练回复（学习者实际看到的文字）：
{{response}}

必须做到：
{{must}}

不能出现：
{{fail}}

评分（1–5 的整数）：
5 - 每条“必须做到”都做到了，没有出现任何“不能出现”的情况
4 - 基本做到，只有一条“必须做到”做得不完整，没有出现“不能出现”的情况
3 - 有一条“必须做到”没做到，或回复有明显不足，但没有出现“不能出现”的情况
2 - 出现了一条“不能出现”的情况
1 - 出现了多条“不能出现”的情况，或回复答非所问
只根据回复内容判断，不猜测系统内部。

输出格式（JSON）：
{"result": <1 到 5 的整数>, "reason": "<一两句中文说明依据>"}
"""


def single_turn_rows():
    rows = []
    for line in (ROOT / "evals/cases.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        if len(case["turns"]) != 1 or "{{" in case["turns"][0]:
            continue
        expect = case["expect"]
        rows.append({"case_id": case["id"], "layer": case["layer"], "query": case["turns"][0],
                     "must": "\n".join(f"- {item}" for item in expect.get("must", [])) or "（无）",
                     "fail": "\n".join(f"- {item}" for item in expect.get("fail", [])) or "（无）"})
    return rows


def ensure_evaluator(project_client):
    from azure.ai.projects.models import EvaluatorCategory, EvaluatorDefinitionType

    return project_client.beta.evaluators.create_version(
        name=EVALUATOR,
        evaluator_version={
            "name": EVALUATOR,
            "categories": [EvaluatorCategory.QUALITY],
            "display_name": "Edu-agent 用例要求",
            "description": "按每条用例的“必须做到 / 不能出现”给学习教练的回复打分（1–5）",
            "definition": {
                "type": EvaluatorDefinitionType.PROMPT,
                "prompt_text": PROMPT,
                "init_parameters": {
                    "type": "object",
                    "properties": {"deployment_name": {"type": "string"}, "threshold": {"type": "number"}},
                    "required": ["deployment_name", "threshold"],
                },
                "data_schema": {
                    "type": "object",
                    "properties": {name: {"type": "string"} for name in ("query", "response", "must", "fail")},
                    "required": ["query", "response", "must", "fail"],
                },
                "metrics": {"must_fail": {"type": "ordinal", "desirable_direction": "increase",
                                          "min_value": 1, "max_value": 5}},
            },
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--multi-agent", default=MULTI_AGENT, help="多 agent 托管 agent 的名字")
    parser.add_argument("--single-agent", default=SINGLE_AGENT, help="单 agent 托管 agent 的名字")
    parser.add_argument("--judge-model", default="", help="判分模型部署名，默认 AZURE_AI_MODEL_DEPLOYMENT_NAME")
    parser.add_argument("--wait", action="store_true", help="等两次评估跑完并打印结果")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / "agent/.env")
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    judge_model = args.judge_model or os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    project_client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    client = project_client.get_openai_client()

    rows = single_turn_rows()
    version = f"{datetime.now():%Y%m%d%H%M}"
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "edu-agent-single-turn.jsonl"
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        dataset = project_client.datasets.upload_file(name="edu-agent-single-turn", version=version,
                                                       file_path=str(path))
    print(f"数据集：{len(rows)} 条单轮用例，版本 {version}")
    evaluator = ensure_evaluator(project_client)
    print(f"评估器：{EVALUATOR}（版本 {getattr(evaluator, 'version', '?')}）")

    evaluation = client.evals.create(
        name=f"单 agent 与多 agent 对比 {version}",
        data_source_config={
            "type": "custom",
            "item_schema": {"type": "object",
                            "properties": {name: {"type": "string"} for name in
                                           ("case_id", "layer", "query", "must", "fail")},
                            "required": ["query", "must", "fail"]},
            "include_sample_schema": True,
        },
        testing_criteria=[{
            "type": "azure_ai_evaluator", "name": EVALUATOR, "evaluator_name": EVALUATOR,
            "data_mapping": {"query": "{{item.query}}", "response": "{{sample.output_text}}",
                             "must": "{{item.must}}", "fail": "{{item.fail}}"},
            "initialization_parameters": {"deployment_name": judge_model, "threshold": 4},
        }],
    )
    runs = {}
    for label, agent_name in (("多 agent", args.multi_agent), ("单 agent", args.single_agent)):
        run = client.evals.runs.create(
            eval_id=evaluation.id, name=f"{label} {agent_name}",
            data_source={
                "type": "azure_ai_target_completions",
                "source": {"type": "file_id", "id": dataset.id},
                "input_messages": {"type": "template", "template": [
                    {"type": "message", "role": "user", "content": {"type": "input_text", "text": "{{item.query}}"}}]},
                "target": {"type": "azure_ai_agent", "name": agent_name},
            },
        )
        runs[label] = run
        print(f"{label}：评估已开始 {getattr(run, 'report_url', '') or run.id}")

    if not args.wait:
        print("评估在 Foundry 里运行，可以在门户的“评估”页查看和对比。")
        return 0
    pending = dict(runs)
    while pending:
        time.sleep(15)
        for label, run in list(pending.items()):
            current = client.evals.runs.retrieve(run_id=run.id, eval_id=evaluation.id)
            if current.status in ("completed", "failed", "canceled"):
                counts = getattr(current, "result_counts", None)
                print(f"{label}：{current.status}，{counts}")
                print(f"  报告：{getattr(current, 'report_url', '')}")
                pending.pop(label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
