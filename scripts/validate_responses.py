#!/usr/bin/env python3
"""Validate saved final responses offline; never invoke a model or print payloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


DEFAULT_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "src/agent-framework-agent-with-local-tools-responses/response.schema.json"
)


class StrictJSONError(ValueError):
    """Contains a fixed diagnostic code, never the rejected input."""


class ConfigurationError(ValueError):
    """Contains a fixed diagnostic code, never a filesystem exception."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise StrictJSONError("non_finite_number")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise StrictJSONError("number_out_of_range")
    return result


def _load_json(text: str) -> Any:
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )


def _error(phase: str, code: str, schema_path: list[Any] | None = None) -> dict:
    return {"phase": phase, "code": code, "schema_path": schema_path or []}


def _parse(text: str) -> tuple[Any, list[dict]]:
    try:
        return _load_json(text), []
    except StrictJSONError as error:
        return None, [_error("parse", str(error))]
    except (ValueError, RecursionError):
        return None, [_error("parse", "invalid_json")]


def validate_text(text: str, schema: dict) -> list[dict]:
    """Return sanitized diagnostics for one final-response text (empty means valid).

    The caller supplies a valid, locally resolved Draft 2020-12 schema. This
    function does no response extraction, network requests, logging, or retries.
    """
    value, errors = _parse(text)
    if errors:
        return errors
    return [
        _error("schema", str(error.validator), list(error.absolute_schema_path))
        for error in Draft202012Validator(schema).iter_errors(value)
    ]


def _extract_response_text(text: str) -> tuple[str | None, list[dict]]:
    envelope, errors = _parse(text)
    if errors:
        return None, errors
    if not isinstance(envelope, dict):
        return None, [_error("extract", "invalid_envelope")]
    if envelope.get("status") != "completed":
        return None, [_error("extract", "response_not_completed")]
    output = envelope.get("output")
    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
        return None, [_error("extract", "invalid_output")]
    messages = [
        item for item in output
        if item.get("type") == "message" and item.get("role") == "assistant"
    ]
    # Tool and reasoning items are deliberately not interpreted or copied.
    if len(messages) != 1:
        code = "missing_assistant_message" if not messages else "ambiguous_assistant_message"
        return None, [_error("extract", code)]
    message = messages[0]
    if message.get("status", "completed") != "completed":
        return None, [_error("extract", "message_not_completed")]
    content = message.get("content")
    if not isinstance(content, list) or any(not isinstance(part, dict) for part in content):
        return None, [_error("extract", "invalid_message_content")]
    if any(part.get("type") == "refusal" for part in content):
        return None, [_error("extract", "refusal")]
    if any(part.get("type") != "output_text" for part in content):
        return None, [_error("extract", "unsupported_message_content")]
    if len(content) != 1:
        code = "missing_final_text" if not content else "ambiguous_final_text"
        return None, [_error("extract", code)]
    final_text = content[0].get("text")
    if not isinstance(final_text, str) or not final_text.strip():
        return None, [_error("extract", "missing_final_text")]
    return final_text, []


def _has_external_reference(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("$ref", "$dynamicRef") and (
                not isinstance(item, str) or not item.startswith("#")
            ):
                return True
            if _has_external_reference(item):
                return True
    elif isinstance(value, list):
        return any(_has_external_reference(item) for item in value)
    return False


def _same_file(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve() or (
        left.exists() and right.exists() and left.samefile(right)
    )


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        # argparse's default error may echo an arbitrary input value.
        raise ConfigurationError("invalid_arguments")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("input", type=Path, help="Saved final text or one candidate per JSONL line")
    parser.add_argument("--format", required=True,
                        choices=("raw-json", "raw-jsonl", "responses-jsonl"))
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--failures", type=Path, required=True,
                        help="New JSONL report containing only sanitized failure metadata")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace an old report; input and schema can never be overwritten")
    try:
        args = parser.parse_args(argv)
        if _same_file(args.failures, args.input) or _same_file(args.failures, args.schema):
            raise ConfigurationError("report_conflicts_with_source")
        if args.failures.exists() and not args.overwrite:
            raise ConfigurationError("report_already_exists")
        try:
            schema = _load_json(args.schema.read_text(encoding="utf-8"))
            if not isinstance(schema, dict) or _has_external_reference(schema):
                raise ConfigurationError("invalid_or_external_schema")
            Draft202012Validator.check_schema(schema)
        except (ValueError, SchemaError, RecursionError):
            raise ConfigurationError("invalid_schema") from None
        raw = args.input.read_bytes()
        if args.format == "raw-json":
            samples = [(None, raw)] if raw.strip() else []
        else:
            samples = [(line, data) for line, data in enumerate(raw.splitlines(), 1) if data.strip()]
        if not samples:
            raise ConfigurationError("no_samples")
        failed = 0
        mode = "w" if args.overwrite else "x"
        with args.failures.open(mode, encoding="utf-8") as report:
            for index, (line, data) in enumerate(samples, 1):
                errors = []
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    errors = [_error("parse", "invalid_utf8")]
                if not errors and args.format == "responses-jsonl":
                    text, errors = _extract_response_text(text)
                if not errors:
                    errors = validate_text(text, schema)
                if errors:
                    failed += 1
                    record = {
                        "sample_index": index,
                        "line": line,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "errors": errors,
                    }
                    report.write(json.dumps(record, ensure_ascii=True) + "\n")
        print(json.dumps({
            "status": "failed" if failed else "passed",
            "samples": len(samples), "passed": len(samples) - failed, "failed": failed,
        }))
        return 1 if failed else 0
    except ConfigurationError as error:
        print(json.dumps({"status": "configuration_error", "code": str(error)}))
        return 2
    except OSError:
        print(json.dumps({"status": "configuration_error", "code": "file_access_error"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
