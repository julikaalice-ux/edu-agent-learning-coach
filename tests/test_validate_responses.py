"""Offline validator tests; no Foundry SDK or model calls are made."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/validate_responses.py"
spec = importlib.util.spec_from_file_location("validate_responses", SCRIPT)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)

# Small schema fixture isolates parsing/extraction from the project's protocol tests.
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "intent", "message", "next_step"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": "1.0"},
        "intent": {
            "type": "object", "required": ["primary", "is_implicit"],
            "additionalProperties": False,
            "properties": {
                "primary": {"enum": list("ABCDEFG")},
                "is_implicit": {"type": "boolean"},
            },
        },
        "message": {"type": "string", "minLength": 1},
        "next_step": {"type": "null"},
    },
}
VALID = {
    "schema_version": "1.0", "intent": {"primary": "E", "is_implicit": False},
    "message": "We can stop here.", "next_step": None,
}


def envelope(text=None):
    return {"status": "completed", "output": [{
        "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": text or json.dumps(VALID)}],
    }]}


class ValidateTextTests(unittest.TestCase):
    def test_valid_final_text(self):
        self.assertEqual(validator.validate_text(json.dumps(VALID), SCHEMA), [])

    def test_rejects_non_json_wrappers(self):
        for text in ("```json\n{}\n```", "Here is the answer: {}", "{} trailing", "{", ""):
            with self.subTest(text=text):
                self.assertEqual(validator.validate_text(text, SCHEMA)[0]["code"], "invalid_json")

    def test_rejects_duplicate_keys_at_every_level(self):
        for text in ('{"message":"a","message":"b"}', '{"intent":{"primary":"A","primary":"B"}}'):
            self.assertEqual(validator.validate_text(text, SCHEMA)[0]["code"], "duplicate_key")

    def test_rejects_non_finite_numbers(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            self.assertEqual(validator.validate_text('{"secret":' + token + '}', SCHEMA)[0]["code"],
                             "non_finite_number")
        self.assertEqual(validator.validate_text('{"n":1e9999}', SCHEMA)[0]["code"], "number_out_of_range")

    def test_schema_errors_do_not_include_values_or_unknown_keys(self):
        payload = {**VALID, "schema_version": "SECRET-VALUE", "SECRET-KEY": "SECRET-VALUE"}
        errors = validator.validate_text(json.dumps(payload), SCHEMA)
        self.assertTrue(errors)
        self.assertTrue(all(error["phase"] == "schema" for error in errors))
        self.assertTrue(all("schema_path" in error for error in errors))
        self.assertNotIn("SECRET", json.dumps(errors))

    def test_top_level_array_fails_schema(self):
        self.assertEqual(validator.validate_text("[]", SCHEMA)[0]["code"], "type")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "input.jsonl"
        self.schema = self.root / "schema.json"
        self.report = self.root / "failures.jsonl"
        self.schema.write_text(json.dumps(SCHEMA), encoding="utf-8")

    def run_check(self, data, mode="raw-jsonl", extra=(), report=None):
        self.source.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = validator.main([
                str(self.source), "--format", mode, "--schema", str(self.schema),
                "--failures", str(report or self.report), *extra,
            ])
        return code, json.loads(output.getvalue())

    def records(self):
        return [json.loads(line) for line in self.report.read_text().splitlines()]

    def test_multiline_raw_response_passes(self):
        code, summary = self.run_check(json.dumps(VALID, indent=2), "raw-json")
        self.assertEqual(code, 0)
        self.assertEqual(summary, {"status": "passed", "samples": 1, "passed": 1, "failed": 0})
        self.assertEqual(self.report.read_text(), "")

    def test_mixed_lines_continue_and_preserve_line_numbers(self):
        bad = '{"message":"PRIVATE-CONTENT","message":"duplicate"}'
        text = json.dumps(VALID) + "\n\n" + bad + "\n[]\n" + json.dumps(VALID)
        code, summary = self.run_check(text)
        self.assertEqual(code, 1)
        self.assertEqual(summary["samples"], 4)
        self.assertEqual(summary["passed"], 2)
        self.assertEqual(summary["failed"], 2)
        records = self.records()
        self.assertEqual([(r["sample_index"], r["line"]) for r in records], [(2, 3), (3, 4)])
        self.assertEqual(records[0]["sha256"], hashlib.sha256(bad.encode()).hexdigest())
        self.assertNotIn("PRIVATE-CONTENT", self.report.read_text())

    def test_empty_inputs_are_configuration_errors(self):
        for mode in ("raw-json", "raw-jsonl", "responses-jsonl"):
            code, summary = self.run_check(" \n\t\n", mode)
            self.assertEqual(code, 2)
            self.assertEqual(summary["code"], "no_samples")
            self.assertFalse(self.report.exists())

    def test_invalid_utf8_does_not_stop_later_samples(self):
        code, summary = self.run_check(b"\xff\n" + json.dumps(VALID).encode())
        self.assertEqual(code, 1)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(self.records()[0]["errors"][0]["code"], "invalid_utf8")

    def test_completed_responses_ignore_tool_and_reasoning_content(self):
        response = envelope()
        response["output"][:0] = [
            {"type": "function_call", "arguments": "CREDENTIAL-SECRET"},
            {"type": "function_call_output", "output": "TOOL-SECRET"},
            {"type": "reasoning", "summary": "PRIVATE-REASONING"},
        ]
        code, summary = self.run_check(json.dumps(response), "responses-jsonl")
        self.assertEqual(code, 0)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(self.report.read_text(), "")

    def test_missing_incomplete_refusal_and_ambiguous_responses_fail(self):
        incomplete = envelope()
        incomplete["status"] = "incomplete"
        refusal = envelope()
        refusal["output"][0]["content"] = [{"type": "refusal", "refusal": "PRIVATE-REFUSAL"}]
        ambiguous = envelope()
        ambiguous["output"].append(ambiguous["output"][0].copy())
        split_text = envelope()
        split_text["output"][0]["content"] *= 2
        cases = [
            ({"status": "completed", "output": []}, "missing_assistant_message"),
            (incomplete, "response_not_completed"), (refusal, "refusal"),
            (ambiguous, "ambiguous_assistant_message"), (split_text, "ambiguous_final_text"),
        ]
        code, summary = self.run_check("\n".join(json.dumps(case) for case, _ in cases), "responses-jsonl")
        self.assertEqual(code, 1)
        self.assertEqual(summary["failed"], len(cases))
        self.assertEqual([r["errors"][0]["code"] for r in self.records()], [code for _, code in cases])
        self.assertNotIn("PRIVATE-REFUSAL", self.report.read_text())

    def test_does_not_guess_convenience_text_or_cli_streams(self):
        cases = [
            json.dumps({"output_text": json.dumps(VALID)}),
            "data: " + json.dumps(envelope()),
            "CLI log: " + json.dumps(envelope()),
        ]
        code, summary = self.run_check("\n".join(cases), "responses-jsonl")
        self.assertEqual(code, 1)
        self.assertEqual(summary["failed"], 3)

    def test_reports_reject_overwrite_by_default_but_explicitly_allow_it(self):
        self.report.write_text("OLD REPORT")
        code, summary = self.run_check(json.dumps(VALID))
        self.assertEqual(code, 2)
        self.assertEqual(summary["code"], "report_already_exists")
        self.assertEqual(self.report.read_text(), "OLD REPORT")
        code, _ = self.run_check(json.dumps(VALID), extra=("--overwrite",))
        self.assertEqual(code, 0)
        self.assertEqual(self.report.read_text(), "")

    def test_never_overwrites_input_or_schema(self):
        for report in (self.source, self.schema):
            schema_before = self.schema.read_bytes()
            code, summary = self.run_check(json.dumps(VALID), report=report, extra=("--overwrite",))
            self.assertEqual(code, 2)
            self.assertEqual(summary["code"], "report_conflicts_with_source")
            self.assertEqual(self.source.read_text(), json.dumps(VALID))
            self.assertEqual(self.schema.read_bytes(), schema_before)

    def test_hardlink_to_source_cannot_be_overwritten(self):
        self.source.write_text(json.dumps(VALID))
        os.link(self.source, self.report)
        code, summary = self.run_check(json.dumps(VALID), extra=("--overwrite",))
        self.assertEqual(code, 2)
        self.assertEqual(summary["code"], "report_conflicts_with_source")
        self.assertEqual(self.source.read_text(), json.dumps(VALID))

    def test_invalid_schema_and_external_refs_are_configuration_errors(self):
        for bad_schema in ({"type": "bogus"}, {"$ref": "https://example.invalid/secret"}):
            self.schema.write_text(json.dumps(bad_schema))
            code, summary = self.run_check(json.dumps(VALID))
            self.assertEqual(code, 2)
            self.assertEqual(summary["status"], "configuration_error")
            self.assertNotIn("secret", json.dumps(summary))

    def test_missing_required_flags_do_not_echo_payload(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = validator.main(["SECRET", "--format", "raw-json"])
        self.assertEqual(code, 2)
        self.assertNotIn("SECRET", output.getvalue())

    def test_cli_exit_status_matches_sample_failure(self):
        self.source.write_text("not json")
        result = subprocess.run([
            sys.executable, str(SCRIPT), str(self.source), "--format", "raw-json",
            "--schema", str(self.schema), "--failures", str(self.report),
        ], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["failed"], 1)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
