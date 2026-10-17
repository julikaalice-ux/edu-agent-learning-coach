"""The single-agent baseline: one Agent Framework agent that uses function tools.

The prompt is assembled from the same rule files the multi-agent version
uses, so both versions follow the same teaching, checking, drawing, question
and notes rules. Only the wrapper (prompts/single-agent.md) is specific to
this version.
"""

import asyncio
from collections import deque
import json
import logging
from pathlib import Path
from time import perf_counter

from learning_coach.adapters.runner import SHARED_PREAMBLE, USAGE_HISTORY, USAGE_KEYS, usage_of
from learning_coach.domain.context import count_tokens
from learning_coach.domain.contracts import ContractError, parse
from learning_coach.service.execution import BUDGET, StageFailure


LOG = logging.getLogger(__name__)
# One turn may use at most this many model round trips inside the tool loop,
# plus one format correction: 11 in all, within the per-turn budget of 12.
MAX_ITERATIONS = 10
FORMAT_CORRECTION = "上次输出未通过结构校验。仅返回符合 output_schema 的 JSON；不附围栏，不再调用工具。"

# (file, top-level sections to keep, subsections to keep inside them or None for all)
RULE_PARTS = (
    ("教学规则（与多 agent 版本的主 agent 相同）", "instructions.md", [
        ("角色与目标", None), ("你是谁，怎么说话", None), ("第一次见面", None), ("每轮执行", None),
        ("画像与参与方式", None), ("根据画像选择教学方式", None), ("意图识别", None),
        ("按意图执行（跨轮进行）", None), ("可见内容与 JSON 协议", None)]),
    ("核对规则（与核对 agent 相同，用于 record_claims）", "agents/source-check.md", [
        ("资料证据核查（无 phase）", ("选择断言", "证据规则", "每条断言的 status"))]),
    ("画图规则（与画图 agent 相同，用于 draw_visual 的 spec）", "agents/visual-designer.md", [
        ("事实依据", None), ("选择形式（依次检查，选第一个符合的）", None), ("语言与长度", None), ("严格输出", None)]),
    ("画完自查（与核对 agent 审图时的检查相同）", "agents/source-check.md", [
        ("visual_review：教学图规格审阅", ("五项检查（checks，均为 boolean）", "按 kind 的重点"))]),
    ("出题规则（与出题 agent 相同，用于 ask_question 和 task=write_question）", "agents/quiz-writer.md", [
        ("输入", None), ("考什么", None), ("题干", None), ("选项和错因标注", None), ("解析", None),
        ("什么时候请求配图", None), ("不重复", None)]),
    ("笔记规则（与笔记 agent 相同，用于 task=compile_notes）", "agents/note-organizer.md", [
        ("输入", None), ("通用规则", None), ("profile", None), ("concepts", None), ("error_records", None),
        ("review_plan", None), ("掌握程度", None)]),
)
QUERY_RULE_START = "- 选 research 时"
QUERY_RULE_END = "search_queries 填 null。"


def _sections(text: str, level: int) -> dict[str, str]:
    """Split markdown at headings of exactly this level; values exclude the heading line."""
    marker = "#" * level + " "
    found, name, lines = {}, None, []
    for line in text.splitlines():
        if line.startswith(marker):
            if name is not None:
                found[name] = "\n".join(lines).strip("\n")
            name, lines = line[len(marker):].strip(), []
        elif name is not None:
            if line.startswith("#" * (level - 1) + " ") and level > 1:
                found[name] = "\n".join(lines).strip("\n")
                name, lines = None, []
            else:
                lines.append(line)
    if name is not None:
        found[name] = "\n".join(lines).strip("\n")
    return found


def _demote(text: str) -> str:
    return "\n".join(("#" + line) if line.startswith("#") else line for line in text.splitlines())


def compose_instructions(prompts_dir: Path) -> str:
    """The single agent's prompt: its own wrapper plus the shared rule sections, verbatim."""
    parts = [(prompts_dir / "single-agent.md").read_text(encoding="utf-8").strip()]
    coordinator = (prompts_dir / "instructions.md").read_text(encoding="utf-8")
    start = coordinator.index(QUERY_RULE_START)
    end = coordinator.index(QUERY_RULE_END, start) + len(QUERY_RULE_END)
    parts.append("# 写搜索词（与多 agent 版本的主 agent 相同，用于 search）\n" + coordinator[start:end])
    for title, file_name, wanted in RULE_PARTS:
        top = _sections((prompts_dir / file_name).read_text(encoding="utf-8"), 1)
        body = []
        for name, keep in wanted:
            if name not in top:
                raise ValueError(f"single_agent_prompt_missing_section:{file_name}:{name}")
            text = top[name]
            if keep is not None:
                inner = _sections(text, 2)
                missing = [item for item in keep if item not in inner]
                if missing:
                    raise ValueError(f"single_agent_prompt_missing_section:{file_name}:{missing[0]}")
                body += [f"## {item}\n{inner[item]}" for item in keep]
            else:
                body.append(f"## {name}\n{_demote(text)}")
        parts.append(f"# {title}\n" + "\n\n".join(body))
    return "\n\n".join(parts) + "\n"


def create_single_agent(client, prompts_dir: Path):
    from agent_framework import Agent

    return Agent(name="single", client=client, instructions=SHARED_PREAMBLE + compose_instructions(prompts_dir),
                 default_options={"store": False})


def _tool_schema(value):
    """Model APIs reject some JSON-schema keywords in tool parameters; keep the meaning."""
    if isinstance(value, dict):
        return {("anyOf" if key == "oneOf" else key): _tool_schema(item)
                for key, item in value.items() if key != "$schema"}
    if isinstance(value, list):
        return [_tool_schema(item) for item in value]
    return value


def _as_text(func):
    """Return tool results as JSON text with Chinese kept as is (the SDK would escape it, costing tokens)."""
    async def call(**kwargs):
        return json.dumps(await func(**kwargs), ensure_ascii=False)
    return call


def _counts(response) -> tuple[int, int]:
    """Model round trips (one assistant message each) and tool calls in one agent run."""
    messages = list(getattr(response, "messages", []) or [])
    model = sum(1 for message in messages if message.role == "assistant")
    tools = sum(1 for message in messages for content in (message.contents or [])
                if getattr(content, "type", None) == "function_call")
    return max(model, 1), tools


class SingleAgentRunner:
    """Runs one task on the single agent with the given tools; checks the final JSON.

    A malformed final answer gets one correction in the same conversation,
    with tools disabled, so tool side effects are never repeated. Every call
    is logged with model round trips, tool calls and token usage (never content).
    """

    def __init__(self, agent, *, timeout=150, observer=None):
        self.agent = agent
        self.timeout = timeout
        self.observer = observer
        self.usage = deque(maxlen=USAGE_HISTORY)

    async def __call__(self, task, payload, schema, tools):
        from agent_framework import FunctionTool, Message

        budget = BUDGET.get()
        if budget is not None and budget.remaining <= 1:
            raise StageFailure("call_budget_exhausted")
        function_tools = [FunctionTool(name=spec.name, description=spec.description, func=_as_text(spec.func),
                                       input_model=_tool_schema(spec.parameters)) for spec in tools]
        message = json.dumps({"task": task, "output_schema": schema, "input": payload}, ensure_ascii=False)
        record = {"role": "single", "phase": task, "attempt": 1, "outcome": "cancelled",
                  "model_calls": 0, "tool_calls": 0, "estimated_input_tokens": count_tokens(message),
                  **dict.fromkeys(USAGE_KEYS)}
        started = perf_counter()

        def add(response):
            model, tool_calls = _counts(response)
            record["model_calls"] += model
            record["tool_calls"] += tool_calls
            for key, value in usage_of(response).items():
                if value is not None:
                    record[key] = (record[key] or 0) + value

        try:
            response = await asyncio.wait_for(
                self.agent.run(message, tools=function_tools) if function_tools else self.agent.run(message),
                timeout=self.timeout)
            add(response)
            try:
                result = parse(response.text, schema)
                record["outcome"] = "ok"
                return result
            except ContractError:
                LOG.info("agent_stage_invalid role=single attempt=1")
                record["attempt"] = 2
            history = [Message("user", [message]), *response.messages, Message("user", [FORMAT_CORRECTION])]
            options = {"tool_choice": "none"}
            retry = await asyncio.wait_for(
                self.agent.run(history, tools=function_tools, options=options) if function_tools
                else self.agent.run(history, options=options), timeout=self.timeout)
            add(retry)
            try:
                result = parse(retry.text, schema)
            except ContractError:
                record["outcome"] = "invalid_output"
                raise StageFailure("invalid_stage_output") from None
            record["outcome"] = "ok"
            return result
        except StageFailure:
            raise
        except Exception as error:
            record["outcome"] = "provider_or_timeout_failure"
            # Never log provider exceptions: they can contain requests or credentials.
            LOG.warning("agent_stage_failed role=single error_type=%s", type(error).__name__)
            raise StageFailure("provider_or_timeout_failure") from None
        finally:
            if budget is not None:
                budget.remaining -= max(record["model_calls"], 1)
            record["elapsed_ms"] = round((perf_counter() - started) * 1000)
            self.usage.append(record)
            LOG.info("agent_stage %s", " ".join(f"{key}={value}" for key, value in record.items()))
            if self.observer is not None:
                self.observer(record)
