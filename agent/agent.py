"""HarborCart checkout-outage investigation agent on Claude Opus 5.5.

Every cost, token count, and tool result this module reports comes from a
live Messages API response.

Request shape, held constant for the whole run so the prompt cache survives:
  - system prompt and tools (both carry cache_control), plus top-level
    automatic caching so the growing message history is cached too
  - output_config: effort "medium", one task_budget for the whole loop, and
    the incident-report JSON schema. Changing output_config.format mid-run
    would invalidate the cache, so the schema is set from the first request.
  - thinking.display "updates", so progress notes between tool calls arrive
    as readable text instead of empty thinking blocks.

Effort is raised to "high" with a per-message system message only when the
evidence stays ambiguous: after at least one counterfactual replay, while the
latest recorded hypothesis is still below "high" confidence.
"""
from __future__ import annotations

import base64
import json
import os
import time

import anthropic
from dotenv import load_dotenv

from . import tools as tool_impl
from .cost import MODEL, CostLedger
from .schema import INCIDENT_REPORT_SCHEMA

load_dotenv()

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(HERE, "assets")
EVIDENCE_DIR = os.path.join(HERE, "evidence")

BETAS = [
    "task-budgets-2026-03-13",
    "mid-conversation-output-config-2026-07-01",
    "thinking-display-updates-2026-08-18",
]
TASK_BUDGET_TOKENS = 80_000
MAX_TOKENS = 16_000
MAX_TURNS = 40
MAX_SECONDS = 900
MAX_COST_USD = 3.00
MAX_REPORT_NUDGES = 2

RETRY_LIBRARY_DOMAINS = ["urllib3.readthedocs.io", "requests.readthedocs.io"]

with open(os.path.join(EVIDENCE_DIR, "alert.json")) as f:
    ALERT = json.load(f)

SYSTEM_PROMPT = f"""You are the on-call incident investigator for HarborCart, an
e-commerce checkout service. An alert fired at {ALERT['fired_at']}:
"{ALERT['alert']}" on {ALERT['instance']}.

Your evidence, and nothing else:
1. query_app_logs, query_traces, query_metrics: bounded, read-only queries over
   the logs, request traces, and metrics for this incident. Call them directly
   or from code execution when you need to scan many windows and only keep a
   summary.
2. get_deployment_context: the latest deployment's metadata, its diff, and
   the service runbook.
3. A monitoring dashboard screenshot and an architecture diagram, attached to
   the first message. Treat what you read from the images as hypotheses and
   check them against query_metrics.
4. web_search, limited to the HTTP retry library's documentation. Use it only
   if you need to confirm how a retry setting behaves. Documentation tells
   you what the software should do; the incident evidence tells you what
   HarborCart did. Keep the two separate.

Work like this:
- Keep competing explanations open. For each serious one, look for the
  observation that would make it unlikely, not only for support.
- Record each working hypothesis with record_hypothesis, including where the
  evidence came from.
- Separate the TRIGGER (the event that started it), the ROOT CAUSE (the
  condition that turned it into a customer-facing outage), and the FAILURE
  MODE (what customers experienced).
- Before you report, test your root cause with run_counterfactual_replay. It
  reruns the same traffic with one change applied. A root cause you have not
  tested this way is not verified; if the replay does not support it, say
  the verdict is inconclusive.

Before each tool call, write one short status line for the on-call engineer:
what you just learned and what you are checking next.

Your final answer is the structured incident report."""

INITIAL_TEXT = (
    f"Alert: {ALERT['alert']} on {ALERT['instance']}, fired at {ALERT['fired_at']} "
    f"({ALERT['window_seconds']}-second window). Attached: the production monitoring "
    "dashboard for the incident window and the checkout path's architecture diagram. "
    "Investigate and determine the trigger, the root cause, and the failure mode."
)

REPORT_NUDGE = (
    "Before the report is accepted, test your root cause with run_counterfactual_replay "
    "and then return the structured incident report."
)


def _b64_image(filename: str) -> str:
    with open(os.path.join(ASSETS_DIR, filename), "rb") as f:
        return base64.b64encode(f.read()).decode()


def build_tools() -> list[dict]:
    tools = [
        {"type": "code_execution_20260120", "name": "code_execution"},
        {
            "type": "web_search_20260318",
            "name": "web_search",
            "allowed_domains": RETRY_LIBRARY_DOMAINS,
            "max_uses": 3,
            "response_inclusion": "excluded",
        },
        {
            "name": "get_deployment_context",
            "description": "Return the latest deployment's metadata, its code diff, and the service runbook. Takes no arguments.",
            "strict": True,
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "record_hypothesis",
            "description": "Log a working hypothesis with its confidence and the evidence behind it, for the investigation's audit trail.",
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "hypothesis": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "supporting_evidence": {"type": "string"},
                    "evidence_source": {
                        "type": "string",
                        "enum": ["dashboard_image", "metrics", "logs", "traces", "diff", "docs", "replay"],
                        "description": "The main source this hypothesis rests on.",
                    },
                },
                "required": ["hypothesis", "confidence", "supporting_evidence", "evidence_source"],
                "additionalProperties": False,
            },
        },
        {
            "name": "run_counterfactual_replay",
            "description": (
                "Rerun the incident's traffic through the same simulation that produced the "
                "evidence, with only the changes you set applied, and report 503s by endpoint type "
                "and cause, seconds the DB pool was saturated, and p95 latency. "
                "Use it to test a root-cause claim. The deployed configuration is: new retry policy, "
                "gateway burst present, pool_capacity 15, connection held through the gateway call."
            ),
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "revert_retry_policy": {"type": "boolean", "description": "Restore the pre-deploy retry policy."},
                    "remove_gateway_burst": {"type": "boolean", "description": "Remove the payment gateway's 503 burst."},
                    "pool_capacity": {"type": "integer", "description": "DB connections per instance (deployed value: 15)."},
                    "release_connection_before_gateway": {
                        "type": "boolean",
                        "description": "Release the DB connection before calling the gateway and reacquire it to commit.",
                    },
                },
                "required": ["revert_retry_policy", "remove_gateway_burst", "pool_capacity",
                             "release_connection_before_gateway"],
                "additionalProperties": False,
            },
        },
        {
            "name": "query_app_logs",
            "description": (
                "Query log lines from checkout-api, payment-gateway, inventory-service, and "
                "web-frontend in a UTC window (ISO 8601). All arguments optional. Returns at most "
                "200 rows plus the total count."
            ),
            "allowed_callers": ["direct", "code_execution_20260120"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "service": {"type": "string"},
                    "level": {"type": "string", "description": "INFO, WARN, or ERROR"},
                    "contains": {"type": "string"},
                },
            },
        },
        {
            "name": "query_traces",
            "description": "Query per-request traces with span breakdowns in a UTC window, optionally by outcome (200, 503_gateway, 503_pool_timeout), path (/checkout, /cart, /orders/{id}), or request_id. Returns at most 200 rows plus the total count.",
            "allowed_callers": ["direct", "code_execution_20260120"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "outcome": {"type": "string"},
                    "path": {"type": "string"},
                    "request_id": {"type": "string"},
                },
            },
        },
        {
            "name": "query_metrics",
            "description": "Query one metric's 5-second series in a UTC window, downsampled to 60 points. Metrics: checkout_p95_latency_ms, db_pool_in_use, checkout_5xx_rate, payment_gateway_5xx_rate, cpu_percent.",
            "allowed_callers": ["direct", "code_execution_20260120"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "metric_name": {"type": "string"},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                },
                "required": ["metric_name"],
            },
        },
    ]
    tools[-1] = dict(tools[-1], cache_control={"type": "ephemeral"})
    return tools


def _initial_user_message() -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": INITIAL_TEXT},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": _b64_image("monitoring_dashboard.png")}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": _b64_image("architecture_diagram.png")}},
        ],
    }


def _is_programmatic(block) -> bool:
    caller = getattr(block, "caller", None)
    return caller is not None and getattr(caller, "type", "direct") != "direct"


def run_investigation(max_turns: int = MAX_TURNS, max_seconds: float = MAX_SECONDS,
                      max_cost_usd: float = MAX_COST_USD, allow_escalation: bool = True):
    """Generator of progress events. The last event has type 'done'."""
    client = anthropic.Anthropic()
    tools = build_tools()
    ledger = CostLedger()
    tool_impl.reset_run_state()
    start = time.time()

    messages = [_initial_user_message()]
    container_id: str | None = None
    stats = {
        "direct_tool_calls": 0, "ptc_tool_calls": 0,
        "direct_result_bytes": 0, "ptc_result_bytes": 0, "sandbox_output_bytes": 0,
        "code_executions": 0, "progress_updates": 0,
    }
    escalated_at_turn: int | None = None
    nudges = 0
    report = None
    stop = "max_turns"
    call_log = []

    def elapsed() -> float:
        return round(time.time() - start, 1)

    request = dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=tools,
        cache_control={"type": "ephemeral"},
        thinking={"type": "adaptive", "display": "updates"},
        output_config={
            "effort": "medium",
            "task_budget": {"type": "tokens", "total": TASK_BUDGET_TOKENS},
            "format": {"type": "json_schema", "schema": INCIDENT_REPORT_SCHEMA},
        },
        betas=BETAS,
    )

    yield {"t": 0.0, "type": "started", "model": MODEL, "effort": "medium",
           "task_budget": TASK_BUDGET_TOKENS}

    turn = 0
    for turn in range(1, max_turns + 1):
        if time.time() - start > max_seconds:
            stop = "time_limit"
            break
        if ledger.total > max_cost_usd:
            stop = "cost_limit"
            break

        kwargs = dict(request, messages=messages)
        if container_id:
            kwargs["container"] = container_id
        resp = client.beta.messages.create(**kwargs)

        call = ledger.add(f"turn-{turn}", resp.usage)
        call_log.append({"turn": turn, "stop_reason": resp.stop_reason, "input_tokens": call.input_tokens,
                         "cache_read_tokens": call.cache_read_tokens, "cache_write_tokens": call.cache_write_tokens,
                         "output_tokens": call.output_tokens, "web_searches": call.web_searches,
                         "cost": call.cost, "effort": "high" if escalated_at_turn else "medium",
                         "blocks": [getattr(b, "type", "?") for b in resp.content]})
        yield {"t": elapsed(), "type": "usage", "turn": turn, "cost": call.cost,
               "running_total": ledger.total, "cache_read": call.cache_read_tokens,
               "cache_write": call.cache_write_tokens, "uncached_input": call.input_tokens,
               "output": call.output_tokens}

        if getattr(resp, "container", None):
            container_id = resp.container.id

        tool_results, text_parts = [], []
        any_programmatic = False
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "thinking" and getattr(block, "thinking", ""):
                stats["progress_updates"] += 1
                yield {"t": elapsed(), "type": "progress", "text": block.thinking}
            elif btype == "text":
                text_parts.append(block.text)
            elif btype == "server_tool_use" and getattr(block, "name", "") == "code_execution":
                stats["code_executions"] += 1
            elif btype and btype.endswith("code_execution_tool_result"):
                stats["sandbox_output_bytes"] += len(json.dumps(block.model_dump().get("content", {})))
            elif btype == "tool_use":
                via_ptc = _is_programmatic(block)
                any_programmatic = any_programmatic or via_ptc
                yield {"t": elapsed(), "type": "tool_call", "name": block.name,
                       "input": block.input, "via_ptc": via_ptc}
                try:
                    result = tool_impl.ALL_TOOL_EXECUTORS[block.name](**(block.input or {}))
                    content, is_error = json.dumps(result), False
                except Exception as exc:  # noqa: BLE001 - errors go back to Claude as tool results
                    result, content, is_error = None, json.dumps({"error": str(exc)}), True
                size = len(content)
                if via_ptc:
                    stats["ptc_tool_calls"] += 1
                    stats["ptc_result_bytes"] += size
                else:
                    stats["direct_tool_calls"] += 1
                    stats["direct_result_bytes"] += size
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                     "content": content, "is_error": is_error})
                yield {"t": elapsed(), "type": "tool_result", "name": block.name,
                       "bytes": size, "via_ptc": via_ptc, "is_error": is_error}
                if block.name == "run_counterfactual_replay" and result:
                    yield {"t": elapsed(), "type": "replay", "result": result}
                if block.name == "record_hypothesis" and not is_error:
                    yield {"t": elapsed(), "type": "hypothesis", "entry": dict(block.input)}

        messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})

        if resp.stop_reason == "refusal":
            stop = "refusal"
            break
        if resp.stop_reason == "pause_turn":
            continue
        if resp.stop_reason == "tool_use" and tool_results:
            hypotheses = tool_impl.RUN_STATE["hypotheses"]
            ambiguous = (tool_impl.RUN_STATE["replays"] and hypotheses
                         and hypotheses[-1]["confidence"] != "high")
            if allow_escalation and escalated_at_turn is None and ambiguous and not any_programmatic:
                escalated_at_turn = turn
                messages.append({"role": "system", "content": [], "output_config": {"effort": "high"}})
                yield {"t": elapsed(), "type": "escalating_effort", "to": "high", "turn": turn,
                       "reason": f"replay run, latest hypothesis confidence {hypotheses[-1]['confidence']}"}
            messages.append({"role": "user", "content": tool_results})
            continue

        text = "".join(text_parts).strip()
        try:
            candidate = json.loads(text) if text else None
        except json.JSONDecodeError:
            candidate = None
        if resp.stop_reason == "end_turn" and candidate and tool_impl.RUN_STATE["replays"]:
            report = candidate
            stop = "report"
            break
        if resp.stop_reason == "max_tokens":
            stop = "max_tokens"
            break
        if nudges >= MAX_REPORT_NUDGES:
            stop = "no_valid_report"
            break
        nudges += 1
        yield {"t": elapsed(), "type": "nudge", "text": REPORT_NUDGE}
        messages.append({"role": "user", "content": REPORT_NUDGE})

    yield {
        "t": elapsed(),
        "type": "done",
        "stop": stop,
        "report": report or {"error": f"run ended without a valid report ({stop})"},
        "cost": ledger.breakdown(),
        "turns": turn,
        "escalated_at_turn": escalated_at_turn,
        "nudges": nudges,
        "hypotheses": list(tool_impl.RUN_STATE["hypotheses"]),
        "replays": list(tool_impl.RUN_STATE["replays"]),
        "calls": call_log,
        "task_budget": TASK_BUDGET_TOKENS,
        "allow_escalation": allow_escalation,
        **stats,
    }
