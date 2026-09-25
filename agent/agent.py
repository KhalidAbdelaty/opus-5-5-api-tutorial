"""HarborCart checkout-outage investigation on Claude Opus 5.5.

Every cost, token count, and tool result this module reports comes from a
live Messages API response. The run has four phases:

1. Investigation (client.beta.messages, effort "medium"). Vision, programmatic
   tool calling over logs, traces, and metrics, direct control tools that
   write to the evidence ledger, and web search limited to the retry
   library's docs. There is no output_config.format here: web search results
   always carry citations, and citations cannot be combined with structured
   outputs. The phase ends when Claude calls finish_investigation and the
   ledger gates pass. A task budget applies to this phase only; pilot runs
   omit it so its size can be measured.
2. Replay planning. The evidence snapshot, frozen before any replay, goes to
   a request with REPLAY_PLAN_SCHEMA and no tools at effort "medium". That
   plan is the one the application runs.
3. Replay. The application runs the medium plan with sim.simulate(), the
   same function that generated the evidence. Claude cannot call it.
4. Reporting (client.messages). The ledger with replay outputs goes to a
   request with INCIDENT_REPORT_SCHEMA and no tools or web search. The
   application checks every cited evidence ID before accepting the report.

Evaluation only: after the production path, the same frozen snapshot is sent
again with a per-message effort of "high". That plan is saved and compared
with the medium plan but never run and never shown to the other request.

Cost control is a stop threshold, not a hard cap. Before each request the
application checks the spend so far and launches nothing new once it is
reached; a request already in flight can still take the total past it.
"""
from __future__ import annotations

import base64
import json
import math
import os
import time

import anthropic
from dotenv import load_dotenv

from . import tools as tool_impl
from .cost import MODEL, CostLedger
from .evidence import MIN_RED_HERRINGS_CHECKED, NO_ALTERNATIVE, RED_HERRINGS, EvidenceLedger
from .schema import INCIDENT_REPORT_SCHEMA, MAX_REPLAY_SCENARIOS, REPLAY_PLAN_SCHEMA

load_dotenv()

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.environ.get("HARBORCART_ASSETS_DIR") or os.path.join(HERE, "assets")
EVIDENCE_DIR = os.path.join(HERE, "evidence")

BETA_TASK_BUDGETS = "task-budgets-2026-03-13"
BETA_THINKING_UPDATES = "thinking-display-updates-2026-08-18"
BETA_PER_MESSAGE_EFFORT = "mid-conversation-output-config-2026-07-01"

MIN_TASK_BUDGET = 20_000
TASK_BUDGET_TOKENS: int | None = 20_000
MAX_TOKENS = 16_000
MAX_TURNS = 40
MAX_SECONDS = 900
STOP_THRESHOLD_USD = 1.00
MAX_FINISH_NUDGES = 2
MAX_REPORT_ATTEMPTS = 2

RETRY_LIBRARY_DOMAINS = ["urllib3.readthedocs.io", "requests.readthedocs.io"]

with open(os.path.join(EVIDENCE_DIR, "alert.json")) as f:
    ALERT = json.load(f)

ALTERNATIVE_LABELS = {
    "inventory_warning": "the inventory-service warning",
    "frontend_warning": "the web-frontend warning",
    "cpu_saturation": "host CPU saturation",
}

INVESTIGATION_PROMPT = f"""You are the on-call incident investigator for HarborCart, an
e-commerce checkout service. An alert fired at {ALERT['fired_at']}:
"{ALERT['alert']}" on {ALERT['instance']}.

Your evidence, and nothing else:
1. query_app_logs, query_traces, query_metrics: bounded, read-only queries over
   the logs, request traces, and metrics for this incident. They can only be
   called from code execution. Each result is JSON with a "source_id". Keep
   raw rows in the sandbox, print only the summary you need, and print the
   source_id next to it so you can cite it.
2. get_deployment_context: the latest deployment's metadata, its diff, and
   the service runbook. Call it directly.
3. A monitoring dashboard screenshot and an architecture diagram, attached to
   the first message. Treat what you read from the images as hypotheses and
   check them against query_metrics.
4. web_search, limited to the HTTP retry library's documentation. Use it only
   if you need to confirm how a retry setting behaves. Documentation tells
   you what the software should do; the incident evidence tells you what
   HarborCart did. Keep the two separate. If you rely on a page, record it
   with record_documentation and its URL.

Build the evidence ledger as you go:
- record_finding: one observation and the source_ids it rests on.
- record_hypothesis: a working explanation, its confidence, and source_ids.
- Separate the TRIGGER (the event that started it), the ROOT CAUSE (the
  condition that turned it into a customer-facing outage), and the FAILURE
  MODE (what customers experienced).
- Keep competing explanations open. For each serious one, look for the
  observation that would make it unlikely, not only for support.
- The on-call checklist requires you to examine at least
  {MIN_RED_HERRINGS_CHECKED} of these alternative explanations before you finish:
  {", ".join(ALTERNATIVE_LABELS.values())}. Record each check with
  record_finding, set "alternative" to the one it examines, and cite the
  query result that covers it.

You cannot run replays in this phase. When the evidence is gathered, call
finish_investigation. The application then plans and runs counterfactual
replays from your ledger and writes the report from it.

Before each tool call, write one short status line for the on-call engineer:
what you just learned and what you are checking next."""

INITIAL_TEXT = (
    f"Alert: {ALERT['alert']} on {ALERT['instance']}, fired at {ALERT['fired_at']} "
    f"({ALERT['window_seconds']}-second window). Attached: the production monitoring "
    "dashboard for the incident window and the checkout path's architecture diagram. "
    "Investigate and build the evidence ledger for the trigger, the root cause, and the failure mode."
)

FINISH_NUDGE = "The investigation is not finished. Call finish_investigation once the ledger is complete."

REPLAY_PARAMETERS = (
    "Each replay reruns the incident's traffic through the simulation that produced the evidence, "
    "with only the parameters you set changed:\n"
    "- revert_retry_policy: restore the pre-deploy retry policy\n"
    "- remove_gateway_burst: remove the payment gateway's 503 burst\n"
    "- pool_capacity: DB connections per instance, 1 to 500 (deployed: 15)\n"
    "- release_connection_before_gateway: release the DB connection before the gateway call and "
    "reacquire it to commit\n"
    "Each replay reports 503s by endpoint type and cause, seconds the DB pool was saturated, and p95 "
    "latency. The as-deployed baseline (no changes) runs automatically."
)

PLANNER_PROMPT = f"""You design counterfactual replays for the HarborCart checkout incident.
You receive an evidence snapshot: query sources, findings, hypotheses, and
documentation notes, each with an ID. Use nothing else.

{REPLAY_PARAMETERS}

Design the smallest set of replays, at most {MAX_REPLAY_SCENARIOS}, that can tell
the leading root-cause hypothesis apart from its strongest competitors.
Prefer scenarios that change one parameter. For each, state what the output
should look like if the hypothesis is true and if it is false."""

REPORT_PROMPT = """You write the incident report for the HarborCart checkout incident from a
verified evidence ledger. Use nothing outside the ledger.

- Cite ledger IDs: S (query sources), F (findings), H (hypotheses),
  D (documentation), R (replay outputs). Use only IDs that appear in the ledger.
- The verdict is "verified" only if the replay outputs support the root cause
  when compared with the as-deployed baseline. Otherwise it is "inconclusive".
- Keep documentation evidence separate from incident evidence.
- For each alternative explanation examined in the ledger, say what the
  evidence showed."""


def _b64_image(filename: str) -> str:
    path = os.path.join(ASSETS_DIR, filename)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} is missing. Generate the runtime images with build_dashboard.py and "
            "build_architecture.py (set HARBORCART_ASSETS_DIR to write them elsewhere).")
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _strict_tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "allowed_callers": ["direct"],
        "input_schema": {"type": "object", "properties": properties, "required": required,
                         "additionalProperties": False},
    }


def build_investigation_tools() -> list[dict]:
    source_ids = {"type": "array", "items": {"type": "string"},
                  "description": "source_id values (S01, S02, ...) from query or deployment results."}
    code_only = ["code_execution_20260120"]
    tools = [
        {"type": "code_execution_20260120", "name": "code_execution"},
        {
            "type": "web_search_20260318",
            "name": "web_search",
            "allowed_domains": RETRY_LIBRARY_DOMAINS,
            "max_uses": 3,
        },
        _strict_tool("get_deployment_context",
                     "Return the latest deployment's metadata, its code diff, and the service runbook, "
                     "with a source_id. Takes no arguments.", {}, []),
        _strict_tool("record_finding",
                     "Add one observation to the evidence ledger, tied to the sources it rests on. Set "
                     "alternative to the alternative explanation this finding examines, or \"none\".",
                     {"claim": {"type": "string"}, "source_ids": source_ids,
                      "alternative": {"type": "string", "enum": [NO_ALTERNATIVE, *RED_HERRINGS]}},
                     ["claim", "source_ids", "alternative"]),
        _strict_tool("record_hypothesis",
                     "Add a working hypothesis to the evidence ledger with its confidence and sources.",
                     {"hypothesis": {"type": "string"},
                      "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                      "supporting_evidence": {"type": "string"}, "source_ids": source_ids},
                     ["hypothesis", "confidence", "supporting_evidence", "source_ids"]),
        _strict_tool("record_documentation",
                     "Add a documentation page returned by web_search in this run, and what it says.",
                     {"url": {"type": "string"}, "claim": {"type": "string"}}, ["url", "claim"]),
        _strict_tool("finish_investigation",
                     "End the investigation. Rejected until the ledger has a hypothesis and the required "
                     "alternative explanations have been examined.",
                     {"summary": {"type": "string"}}, ["summary"]),
        {
            "name": "query_app_logs",
            "description": (
                "Query log lines from checkout-api, payment-gateway, inventory-service, and web-frontend "
                "in a UTC window (ISO 8601). All arguments optional. Returns JSON: source_id, count (all "
                "matches), truncated, services (match count per service), rows (at most 200, each with "
                "timestamp, service, level, message, and request_id when present)."
            ),
            "allowed_callers": code_only,
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
            "description": (
                "Query per-request traces in a UTC window, optionally by outcome (200, 503_gateway, "
                "503_pool_timeout), path (/checkout, /cart, /orders/{id}), or request_id. Returns JSON: "
                "source_id, count, truncated, rows (at most 200, each with request_id, method, path, start, "
                "duration_ms, outcome, and spans with name, duration_ms, and status or attempt)."
            ),
            "allowed_callers": code_only,
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
            "description": (
                "Query one metric's 5-second series in a UTC window, downsampled to 60 points. Metrics: "
                "checkout_p95_latency_ms, db_pool_in_use, checkout_5xx_rate, payment_gateway_5xx_rate, "
                "cpu_percent. Returns JSON: source_id, metric, count, points (each {t, v})."
            ),
            "allowed_callers": code_only,
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


def build_investigation_request(task_budget: int | None) -> dict:
    output_config: dict = {"effort": "medium"}
    betas = [BETA_THINKING_UPDATES]
    if task_budget is not None:
        if task_budget < MIN_TASK_BUDGET:
            raise ValueError(f"task_budget must be at least {MIN_TASK_BUDGET:,} tokens")
        output_config["task_budget"] = {"type": "tokens", "total": task_budget}
        betas.append(BETA_TASK_BUDGETS)
    return dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": INVESTIGATION_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=build_investigation_tools(),
        cache_control={"type": "ephemeral"},
        thinking={"type": "adaptive", "display": "updates"},
        output_config=output_config,
        betas=betas,
    )


def initial_user_message() -> dict:
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


def build_plan_request(snapshot: dict, effort: str) -> dict:
    """Both plan requests share everything except the per-message effort
    message, which only the high-effort evaluation request carries."""
    messages: list[dict] = []
    if effort != "medium":
        messages.append({"role": "system", "content": [], "output_config": {"effort": effort}})
    messages.append({"role": "user", "content": "Evidence snapshot:\n" + json.dumps(snapshot, indent=1)})
    return dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=PLANNER_PROMPT,
        messages=messages,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": REPLAY_PLAN_SCHEMA}},
        betas=[BETA_PER_MESSAGE_EFFORT],
    )


def build_report_request(ledger_view: dict, rejected: tuple[dict, list[str]] | None = None) -> dict:
    messages: list[dict] = [{"role": "user", "content": "Evidence ledger:\n" + json.dumps(ledger_view, indent=1)}]
    if rejected:
        report, problems = rejected
        messages.append({"role": "assistant", "content": json.dumps(report)})
        messages.append({"role": "user", "content": "The application rejected this report:\n- "
                         + "\n- ".join(problems) + "\nReturn a corrected report."})
    return dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=REPORT_PROMPT,
        messages=messages,
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": INCIDENT_REPORT_SCHEMA}},
    )


def derive_task_budget(pilot_spends: list[int], margin: float = 0.25, step: int = 5_000) -> int:
    """Largest pilot spend plus a stated margin, rounded up to `step`."""
    if not pilot_spends:
        raise ValueError("need at least one pilot measurement")
    target = max(pilot_spends) * (1 + margin)
    return max(MIN_TASK_BUDGET, int(math.ceil(target / step) * step))


def execute_tool_call(ledger: EvidenceLedger, name: str, tool_input: dict,
                      caller_type: str | None) -> tuple[dict, bool]:
    """Run one client-side tool call. Caller policy is enforced here, not by
    allowed_callers."""
    error = tool_impl.check_caller(name, caller_type)
    if error:
        return {"error": error}, True
    tool_input = tool_input or {}
    try:
        if name in tool_impl.EVIDENCE_TOOL_EXECUTORS:
            result = tool_impl.EVIDENCE_TOOL_EXECUTORS[name](**tool_input)
            source_id = ledger.add_source(name, tool_input, result, tool_impl.caller_kind(caller_type))
            return {"source_id": source_id, **result}, False
        if name == "record_finding":
            return ledger.record_finding(**tool_input), False
        if name == "record_hypothesis":
            return ledger.record_hypothesis(**tool_input), False
        if name == "record_documentation":
            return ledger.record_documentation(**tool_input), False
        if name == "finish_investigation":
            problems = ledger.finish_problems()
            if problems:
                return {"error": "the ledger is not complete", "problems": problems}, True
            return {"finished": True}, False
    except (TypeError, ValueError) as exc:
        return {"error": str(exc)}, True
    return {"error": f"unknown tool '{name}'"}, True


def validate_replay_plan(plan: dict, ledger: EvidenceLedger) -> tuple[list[dict], list[str]]:
    """Scenarios the application will run, and problems with the rest."""
    problems, runnable, seen = [], [], {tuple(tool_impl.DEPLOYED_SCENARIO.values())}
    known_h = {h.id for h in ledger.hypotheses}
    for h in plan.get("hypothesis_ids", []):
        if h not in known_h:
            problems.append(f"plan cites unknown hypothesis {h}")
    for i, sc in enumerate(plan.get("scenarios", [])):
        params = {k: sc.get(k) for k in tool_impl.DEPLOYED_SCENARIO}
        if i >= MAX_REPLAY_SCENARIOS:
            problems.append(f"scenario '{sc.get('name')}' skipped: more than {MAX_REPLAY_SCENARIOS} scenarios")
            continue
        if not all(isinstance(v, bool) for k, v in params.items() if k != "pool_capacity"):
            problems.append(f"scenario '{sc.get('name')}' skipped: switches must be booleans")
            continue
        pool = params["pool_capacity"]
        if isinstance(pool, bool) or not isinstance(pool, int) or not 1 <= pool <= 500:
            problems.append(f"scenario '{sc.get('name')}' skipped: pool_capacity must be 1 to 500")
            continue
        key = tuple(params.values())
        if key in seen:
            problems.append(f"scenario '{sc.get('name')}' skipped: duplicate of an earlier replay")
            continue
        seen.add(key)
        runnable.append(sc)
    return runnable, problems


def scenario_key(sc: dict) -> str:
    return json.dumps({k: sc.get(k) for k in tool_impl.DEPLOYED_SCENARIO}, sort_keys=True)


def compare_plans(medium: dict | None, high: dict | None) -> dict | None:
    if not medium or not high:
        return None
    m = {scenario_key(s): s["name"] for s in medium.get("scenarios", [])}
    h = {scenario_key(s): s["name"] for s in high.get("scenarios", [])}
    return {
        "medium_scenarios": len(m),
        "high_scenarios": len(h),
        "shared": [json.loads(k) for k in sorted(set(m) & set(h))],
        "medium_only": [json.loads(k) for k in sorted(set(m) - set(h))],
        "high_only": [json.loads(k) for k in sorted(set(h) - set(m))],
        "same_hypothesis_ids": sorted(medium.get("hypothesis_ids", [])) == sorted(high.get("hypothesis_ids", [])),
    }


def _dump(block) -> dict:
    return block.model_dump(mode="json", exclude_none=True)


def _text(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def _parse_json(resp) -> dict | None:
    try:
        return json.loads(_text(resp))
    except json.JSONDecodeError:
        return None


def count_text_tokens(client, texts: list[str]) -> int | None:
    """Token count of the tool-result text Claude saw, via the free token
    counting endpoint. Returns None if counting fails."""
    if not texts:
        return 0
    try:
        full = client.messages.count_tokens(
            model=MODEL, messages=[{"role": "user", "content": "\n".join(texts)}]).input_tokens
        base = client.messages.count_tokens(
            model=MODEL, messages=[{"role": "user", "content": "."}]).input_tokens
    except anthropic.APIError:
        return None
    return max(0, full - base)


class _Limits:
    def __init__(self, costs: CostLedger, spent_before: float, stop_threshold_usd: float,
                 max_seconds: float, start: float):
        self.costs, self.spent_before = costs, spent_before
        self.stop_threshold_usd, self.max_seconds, self.start = stop_threshold_usd, max_seconds, start

    def elapsed(self) -> float:
        return round(time.time() - self.start, 1)

    def blocked(self) -> str | None:
        if self.spent_before + self.costs.total >= self.stop_threshold_usd:
            return "cost_threshold"
        if time.time() - self.start > self.max_seconds:
            return "time_limit"
        return None


def _investigate(client, ledger: EvidenceLedger, costs: CostLedger, limits: _Limits,
                 task_budget: int | None, max_turns: int, call_log: list):
    request = build_investigation_request(task_budget)
    messages = [initial_user_message()]
    container_id: str | None = None
    seen_tool_text: list[str] = []
    stats = {
        "direct_tool_calls": 0, "ptc_tool_calls": 0, "caller_rejections": 0,
        "direct_result_bytes": 0, "ptc_result_bytes": 0, "sandbox_output_bytes": 0,
        "code_executions": 0, "progress_updates": 0, "web_search_results": 0,
    }
    nudges, stop, turn = 0, "max_turns", 0

    for turn in range(1, max_turns + 1):
        blocked = limits.blocked()
        if blocked:
            stop = blocked
            break
        kwargs = dict(request, messages=messages)
        if container_id:
            kwargs["container"] = container_id
        resp = client.beta.messages.create(**kwargs)

        call = costs.add(f"investigation-{turn}", resp.usage, "investigation")
        call_log.append({"phase": "investigation", "turn": turn, "stop_reason": resp.stop_reason,
                         "input_tokens": call.input_tokens, "cache_read_tokens": call.cache_read_tokens,
                         "cache_write_tokens": call.cache_write_tokens, "output_tokens": call.output_tokens,
                         "web_searches": call.web_searches, "cost": call.cost, "effort": "medium",
                         "blocks": [getattr(b, "type", "?") for b in resp.content]})
        yield {"t": limits.elapsed(), "type": "usage", "phase": "investigation", "turn": turn,
               "cost": call.cost, "running_total": limits.spent_before + costs.total,
               "cache_read": call.cache_read_tokens, "cache_write": call.cache_write_tokens,
               "uncached_input": call.input_tokens, "output": call.output_tokens}

        if getattr(resp, "container", None):
            container_id = resp.container.id

        tool_uses = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "thinking" and getattr(block, "thinking", ""):
                stats["progress_updates"] += 1
                yield {"t": limits.elapsed(), "type": "progress", "text": block.thinking}
            elif btype == "server_tool_use" and getattr(block, "name", "") == "code_execution":
                stats["code_executions"] += 1
            elif btype and btype.endswith("code_execution_tool_result"):
                output = json.dumps(_dump(block).get("content", {}))
                stats["sandbox_output_bytes"] += len(output)
                seen_tool_text.append(output)
            elif btype == "web_search_tool_result":
                content = _dump(block).get("content")
                for item in content if isinstance(content, list) else []:
                    if item.get("url"):
                        stats["web_search_results"] += 1
                        ledger.add_retrieved_url(item["url"], item.get("title", ""))
            elif btype == "tool_use":
                tool_uses.append(block)

        tool_uses.sort(key=lambda b: b.name == "finish_investigation")
        tool_results, finished = [], False
        for block in tool_uses:
            caller_type = getattr(getattr(block, "caller", None), "type", None)
            via_ptc = tool_impl.caller_kind(caller_type) == tool_impl.CODE_EXECUTION
            rejected = tool_impl.check_caller(block.name, caller_type) is not None
            yield {"t": limits.elapsed(), "type": "tool_call", "name": block.name,
                   "input": block.input, "via_ptc": via_ptc}
            result, is_error = execute_tool_call(ledger, block.name, block.input, caller_type)
            content = json.dumps(result)
            if via_ptc:
                stats["ptc_tool_calls"] += 1
                stats["ptc_result_bytes"] += len(content)
            else:
                stats["direct_tool_calls"] += 1
                stats["direct_result_bytes"] += len(content)
                seen_tool_text.append(content)
            stats["caller_rejections"] += rejected
            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                 "content": content, "is_error": is_error})
            yield {"t": limits.elapsed(), "type": "tool_result", "name": block.name, "bytes": len(content),
                   "via_ptc": via_ptc, "is_error": is_error, "caller_rejected": rejected,
                   "source_id": result.get("source_id"), "recorded": result.get("recorded"),
                   "error": result.get("error")}
            if not is_error and block.name in ("record_finding", "record_hypothesis", "record_documentation"):
                yield {"t": limits.elapsed(), "type": block.name.replace("record_", ""),
                       "id": result["recorded"], "entry": dict(block.input)}
            if block.name == "finish_investigation" and not is_error:
                finished = True

        messages.append({"role": "assistant", "content": [_dump(b) for b in resp.content]})

        if finished:
            stop = "finished"
            break
        if resp.stop_reason == "refusal":
            stop = "refusal"
            break
        if resp.stop_reason == "pause_turn":
            continue
        if tool_results:
            messages.append({"role": "user", "content": tool_results})
            continue
        if resp.stop_reason == "max_tokens":
            stop = "max_tokens"
            break
        if nudges >= MAX_FINISH_NUDGES:
            stop = "not_finished"
            break
        nudges += 1
        problems = ledger.finish_problems()
        nudge = FINISH_NUDGE + ("" if not problems else " Still missing: " + "; ".join(problems))
        yield {"t": limits.elapsed(), "type": "nudge", "text": nudge}
        messages.append({"role": "user", "content": nudge})

    output_tokens = sum(c.output_tokens for c in costs.calls if c.phase == "investigation")
    tool_result_tokens = count_text_tokens(client, seen_tool_text)
    return {
        "stop": stop,
        "turns": turn,
        "nudges": nudges,
        "task_budget": task_budget,
        "budget_measurement": {
            "output_tokens": output_tokens,
            "tool_result_tokens": tool_result_tokens,
            "total": None if tool_result_tokens is None else output_tokens + tool_result_tokens,
            "counted": "output tokens of every investigation request, plus direct tool results and "
                       "code execution output as Claude saw them; programmatic tool results and "
                       "web search result content are not included",
        },
        **stats,
    }


def _request_plan(client, snapshot: dict, effort: str, costs: CostLedger, call_log: list) -> dict:
    try:
        resp = client.beta.messages.create(**build_plan_request(snapshot, effort))
    except anthropic.APIStatusError as exc:
        return {"effort": effort, "stop_reason": None, "plan": None, "output_tokens": 0, "cost": 0.0,
                "error": f"HTTP {exc.status_code}: {exc.message}"}
    call = costs.add(f"plan-{effort}", resp.usage, f"plan_{effort}")
    call_log.append({"phase": f"plan_{effort}", "turn": 1, "stop_reason": resp.stop_reason,
                     "input_tokens": call.input_tokens, "cache_read_tokens": call.cache_read_tokens,
                     "cache_write_tokens": call.cache_write_tokens, "output_tokens": call.output_tokens,
                     "web_searches": 0, "cost": call.cost, "effort": effort,
                     "blocks": [getattr(b, "type", "?") for b in resp.content]})
    return {"effort": effort, "stop_reason": resp.stop_reason, "plan": _parse_json(resp),
            "output_tokens": call.output_tokens, "cost": call.cost}


def run_investigation(task_budget: int | None = TASK_BUDGET_TOKENS, pilot: bool = False,
                      stop_threshold_usd: float = STOP_THRESHOLD_USD, spent_before: float = 0.0,
                      evaluate_high_plan: bool = True, max_turns: int = MAX_TURNS,
                      max_seconds: float = MAX_SECONDS, client=None):
    """Generator of progress events. The last event has type 'done' and holds
    the full run record. A pilot runs the investigation phase only, without a
    task budget, to measure what the budget should be."""
    client = client or anthropic.Anthropic()
    if pilot:
        task_budget = None
    costs = CostLedger()
    ledger = EvidenceLedger(allowed_doc_domains=RETRY_LIBRARY_DOMAINS)
    limits = _Limits(costs, spent_before, stop_threshold_usd, max_seconds, time.time())
    call_log: list[dict] = []
    record: dict = {"model": MODEL, "pilot": pilot, "task_budget": task_budget,
                    "stop_threshold_usd": stop_threshold_usd, "spent_before": spent_before,
                    "red_herrings": RED_HERRINGS, "min_red_herrings_checked": MIN_RED_HERRINGS_CHECKED}

    yield {"t": 0.0, "type": "started", "model": MODEL, "effort": "medium", "task_budget": task_budget,
           "pilot": pilot}

    yield {"t": limits.elapsed(), "type": "phase", "phase": "investigation"}
    investigation = yield from _investigate(client, ledger, costs, limits, task_budget, max_turns, call_log)
    record["investigation"] = investigation
    snapshot = ledger.snapshot()
    record["evidence_snapshot"] = snapshot
    yield {"t": limits.elapsed(), "type": "snapshot", "stop": investigation["stop"],
           "sources": len(snapshot["sources"]), "findings": len(snapshot["findings"]),
           "hypotheses": len(snapshot["hypotheses"]), "documentation": len(snapshot["documentation"]),
           "alternatives_examined": snapshot["alternatives_examined"]}

    stop = investigation["stop"] if investigation["stop"] != "finished" else None
    if pilot and stop is None:
        stop = "pilot_complete"
    plans: dict = {}
    report, report_problems, attempts = None, [], 0

    if stop is None:
        stop = limits.blocked()
    if stop is None:
        yield {"t": limits.elapsed(), "type": "phase", "phase": "plan_medium"}
        plans["medium"] = _request_plan(client, snapshot, "medium", costs, call_log)
        yield {"t": limits.elapsed(), "type": "plan", **plans["medium"]}
        if not plans["medium"]["plan"]:
            stop = "no_replay_plan"

    if stop is None:
        yield {"t": limits.elapsed(), "type": "phase", "phase": "replay"}
        runnable, plan_problems = validate_replay_plan(plans["medium"]["plan"], ledger)
        plans["medium"]["problems"] = plan_problems
        scenarios = [{"name": "as deployed (baseline)", "purpose": "baseline for every comparison",
                      **tool_impl.DEPLOYED_SCENARIO}] + runnable
        for sc in scenarios:
            result = tool_impl.run_counterfactual_replay(**{k: sc[k] for k in tool_impl.DEPLOYED_SCENARIO})
            replay_id = ledger.add_replay(sc["name"], sc["purpose"], result)
            yield {"t": limits.elapsed(), "type": "replay", "id": replay_id, "name": sc["name"], "result": result}

        yield {"t": limits.elapsed(), "type": "phase", "phase": "report"}
        rejected = None
        while attempts < MAX_REPORT_ATTEMPTS:
            blocked = limits.blocked()
            if blocked:
                stop = blocked
                break
            attempts += 1
            try:
                resp = client.messages.create(**build_report_request(ledger.to_dict(), rejected))
            except anthropic.APIStatusError as exc:
                report_problems = [f"report request failed: HTTP {exc.status_code}: {exc.message}"]
                break
            call = costs.add(f"report-{attempts}", resp.usage, "report")
            call_log.append({"phase": "report", "turn": attempts, "stop_reason": resp.stop_reason,
                             "input_tokens": call.input_tokens, "cache_read_tokens": call.cache_read_tokens,
                             "cache_write_tokens": call.cache_write_tokens, "output_tokens": call.output_tokens,
                             "web_searches": 0, "cost": call.cost, "effort": "medium",
                             "blocks": [getattr(b, "type", "?") for b in resp.content]})
            candidate = _parse_json(resp)
            if candidate is None:
                report_problems = [f"report was not valid JSON (stop_reason {resp.stop_reason})"]
                break
            report_problems = ledger.validate_report(candidate)
            yield {"t": limits.elapsed(), "type": "report_check", "attempt": attempts,
                   "accepted": not report_problems, "problems": report_problems}
            report = candidate
            if not report_problems:
                stop = "report"
                break
            rejected = (candidate, report_problems)
        if stop is None:
            stop = "report_rejected"

    if evaluate_high_plan and "medium" in plans and not limits.blocked():
        yield {"t": limits.elapsed(), "type": "phase", "phase": "plan_high"}
        plans["high"] = _request_plan(client, snapshot, "high", costs, call_log)
        yield {"t": limits.elapsed(), "type": "plan", **plans["high"]}

    replays = [r.result | {"id": r.id, "name": r.scenario_name} for r in ledger.replays]
    record.update({
        "t": limits.elapsed(),
        "type": "done",
        "stop": stop,
        "report": report or {"error": f"run ended without an accepted report ({stop})"},
        "report_accepted": stop == "report",
        "report_attempts": attempts,
        "report_problems": report_problems,
        "replay_plans": plans,
        "plan_comparison": compare_plans((plans.get("medium") or {}).get("plan"),
                                         (plans.get("high") or {}).get("plan")),
        "replays": replays,
        "evidence_ledger": ledger.to_dict(),
        "cost": costs.breakdown(),
        "calls": call_log,
    })
    yield record
