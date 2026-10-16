"""Score collected agent responses against evals/cases.jsonl.

Evidence policy mirrors scripts/validate_responses.py: failure reports record
only case id, turn index and error codes -- never response content.

Input rows (JSONL):
  {"case_id": "INT-G", "turn": 0, "text": "<final assistant text>",
   "allowed_urls": ["https://..."]}   # optional, for urls_from_sources_only

Checks named in a case's expect.auto list:
  strict_json                parse without fences/duplicate keys/non-finite numbers
  schema                     validate against response.schema.json
  intent                     compare primary (and is_implicit when specified)
  next_step_question         next_step is an object with kind == "question"
  next_step_present          next_step is not null
  next_step_null             next_step is null
  next_step_null_allowed     no constraint (documents intent; always passes)
  one_question_per_turn      at most one set of A-D options in next_step.text
  answer_key_not_in_message  no private answer-key marker in visible fields
  quiz_in_next_step_only     option lines in next_step are not repeated in message
  urls_from_sources_only     URLs must be a subset of allowed_urls (else SKIP)
  same_as_first              identical to the first collected response of the case

Usage (from repository root, project env per docs/acceptance-v1.5.md):
  uv run --project agent \
      --locked --group dev python evals/score.py \
      --responses runs/v1.jsonl [--failures failures.jsonl]
"""

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import re
import sys

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = Path(__file__).with_name("cases.jsonl")
DEFAULT_SCHEMA = ROOT / "agent/response.schema.json"

OPTION_LINE = re.compile(r"^[A-D][.、．]\s", re.MULTILINE)
URL = re.compile(r"https?://[^\s)<>]+")
FENCE = re.compile(r"^\s*```", re.MULTILINE)


def _no_dupes(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _no_constant(_value):
    raise ValueError("non_finite_number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non_finite_number")
    return number


def parse_strict(text):
    """Return (value_or_None, codes). Fences are reported but parsing still attempted."""
    if not isinstance(text, str) or not text.strip():
        return None, ["empty_text"]
    codes = ["fenced_output"] if FENCE.search(text) else []
    try:
        value = json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_no_constant,
                           parse_float=_finite_float)
    except ValueError:
        return None, codes + ["invalid_json"]
    return value, codes


def load_cases(path):
    cases = {}
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            case = json.loads(line)
            for field in ("id", "layer", "turns", "expect"):
                if field not in case:
                    raise ValueError(f"cases:{line_number}: missing {field}")
            if "per_turn" in case["expect"] and "score_turns" not in case["expect"]:
                raise ValueError(f"cases:{case['id']}: per_turn requires score_turns")
            cases[case["id"]] = case
    return cases


def expected_intent(case, turn):
    expect = case["expect"]
    if "per_turn" in expect:
        for index, scored in enumerate(expect["score_turns"]):
            if scored == turn:
                return expect["per_turn"][index]
        return None
    if "primary" in expect:
        return expect
    return None


def run_checks(case, turn, value, validator, first_canonical, allowed_urls):
    codes = []
    checks = case["expect"].get("auto", [])

    if "schema" in checks:
        if value is None:
            codes.append("schema_not_checked")
        elif not validator.is_valid(value):
            codes.append("schema_mismatch")

    if "intent" in checks and value is not None:
        wanted = expected_intent(case, turn)
        if wanted is not None:
            actual = value.get("intent", {})
            if actual.get("primary") != wanted.get("primary"):
                codes.append("intent_primary_mismatch")
            if "is_implicit" in wanted and actual.get("is_implicit") != wanted["is_implicit"]:
                codes.append("intent_implicit_mismatch")

    next_step = value.get("next_step") if isinstance(value, dict) else None
    if "next_step_question" in checks and (not isinstance(next_step, dict) or next_step.get("kind") != "question"):
        codes.append("next_step_not_question")
    if "next_step_present" in checks and next_step is None:
        codes.append("next_step_missing")
    if "next_step_null" in checks and next_step is not None:
        codes.append("next_step_not_null")

    step_text = next_step.get("text", "") if isinstance(next_step, dict) else ""
    message = value.get("message", "") if isinstance(value, dict) else ""
    if "one_question_per_turn" in checks:
        if len(OPTION_LINE.findall(step_text)) > 4:
            codes.append("multiple_questions_one_turn")
    if "answer_key_not_in_message" in checks:
        if "ANSWER_KEY_PRIVATE" in message + "\n" + step_text:
            codes.append("answer_key_leaked")
    if "quiz_in_next_step_only" in checks:
        options = [line for line in step_text.splitlines() if OPTION_LINE.match(line)]
        if options and all(any(opt.strip() in line for line in message.splitlines()) for opt in options):
            codes.append("quiz_repeated_in_message")
    if "urls_from_sources_only" in checks:
        if allowed_urls is None:
            codes.append("manual_check_required")
        else:
            found = set(URL.findall(message + "\n" + step_text))
            if not found.issubset(set(allowed_urls)):
                codes.append("unretrieved_url")
    if "same_as_first" in checks:
        if first_canonical is None or not isinstance(value, dict) or \
                json.dumps(value, ensure_ascii=False, sort_keys=True) != first_canonical:
            codes.append("replay_response_differs")
    return codes


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--responses", required=True, help="collected responses JSONL")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    parser.add_argument("--failures", default=None, help="write failure report JSONL")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    rows = []
    with open(args.responses, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            for field in ("case_id", "turn", "text"):
                if field not in row:
                    raise ValueError(f"responses:{line_number}: missing {field}")
            rows.append(row)
    if not rows:
        print("no samples", file=sys.stderr)
        return 2

    first_canonical = {}
    for row in rows:
        if row["case_id"] not in first_canonical:
            value, _ = parse_strict(row["text"])
            first_canonical[row["case_id"]] = (
                json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, dict) else None)

    failures = []
    totals = defaultdict(lambda: [0, 0])  # layer -> [passed, failed]
    for row in rows:
        case = cases.get(row["case_id"])
        if case is None:
            failures.append({"case_id": row["case_id"], "turn": row["turn"], "codes": ["unknown_case_id"]})
            continue
        checks = case["expect"].get("auto", [])
        value, parse_codes = parse_strict(row["text"])
        # Parse failures only count against cases that claim strict_json; cases
        # without it still fail closed via schema_not_checked (schema is claimed
        # by every case in this set).
        counted = [code for code in parse_codes if "strict_json" in checks]
        all_codes = counted + run_checks(case, row["turn"], value, validator,
                                         first_canonical.get(row["case_id"]), row.get("allowed_urls"))
        manual_only = all_codes and all(code == "manual_check_required" for code in all_codes)
        if not all_codes:
            status = "PASS"
            totals[case["layer"]][0] += 1
        elif manual_only:
            status = "SKIP"
        else:
            status = "FAIL"
            totals[case["layer"]][1] += 1
            failures.append({"case_id": row["case_id"], "turn": row["turn"], "codes": all_codes})
        print(f"{status} {row['case_id']} turn={row['turn']}" +
              (f" codes={','.join(all_codes)}" if all_codes else ""))

    print("\nsummary by layer (auto checks):")
    passed = failed = 0
    for layer in sorted(totals):
        ok, bad = totals[layer]
        passed, failed = passed + ok, failed + bad
        print(f"  {layer}: {ok} passed, {bad} failed")
    print(f"total: {passed} passed, {failed} failed, {len(rows)} rows")

    if args.failures and failures:
        Path(args.failures).write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in failures), encoding="utf-8")
        print(f"failures written to {args.failures}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
