"""Bounded, deterministic tool implementations for the HarborCart investigation
agent.

Design principle: Claude decides what
evidence it needs; this module decides what it is allowed to access. Every
tool here is read-only against a fixed evidence packet on disk, takes a
narrow set of arguments, and returns plain JSON-serialisable data. None of
them can write, delete, or reach outside `evidence/`.

Two tool groups are exposed to the model (see agent.py):
  - STRICT tools (get_deployment_context, run_counterfactual_replay,
    record_hypothesis): declared with strict=True, so they are never used
    with programmatic tool calling.
  - FAN-OUT tools (query_app_logs, query_traces, query_metrics): declared
    with allowed_callers including "code_execution_20260120", so Claude's
    own sandboxed code can call them directly and in parallel when it wants
    to scan many time windows or services at once. Strict schemas do not
    apply on that path, so each function validates its own arguments.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVIDENCE_DIR = os.path.join(HERE, "evidence")
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import sim  # noqa: E402

_CACHE: dict[str, object] = {}
RUN_STATE: dict[str, list] = {"hypotheses": [], "replays": []}


def reset_run_state() -> None:
    RUN_STATE["hypotheses"] = []
    RUN_STATE["replays"] = []


def _load_jsonl(name: str) -> list[dict]:
    if name not in _CACHE:
        path = os.path.join(EVIDENCE_DIR, name)
        with open(path) as f:
            _CACHE[name] = [json.loads(line) for line in f if line.strip()]
    return _CACHE[name]


def _load_json(name: str) -> dict:
    if name not in _CACHE:
        path = os.path.join(EVIDENCE_DIR, name)
        with open(path) as f:
            _CACHE[name] = json.load(f)
    return _CACHE[name]


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _check_str(name: str, value, max_len: int = 64) -> None:
    """Programmatic calls skip strict schema checks, so the app validates here."""
    if value is None:
        return
    if not isinstance(value, str) or len(value) > max_len:
        raise ValueError(f"{name} must be a string of at most {max_len} characters")
    if name.endswith("_time"):
        try:
            _parse_ts(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO 8601 UTC timestamp") from exc


LEVEL_ALIASES = {"WARNING": "WARN", "ERR": "ERROR"}


def _in_window(ts: str, start_time: str | None, end_time: str | None) -> bool:
    dt = _parse_ts(ts)
    if start_time and dt < _parse_ts(start_time):
        return False
    if end_time and dt > _parse_ts(end_time):
        return False
    return True


# --------------------------------------------------------------------------
# Fan-out tools: safe to call many times in parallel via PTC
# --------------------------------------------------------------------------

MAX_ROWS = 200


def query_app_logs(start_time: str | None = None, end_time: str | None = None,
                    service: str | None = None, level: str | None = None,
                    contains: str | None = None) -> dict:
    """Query application/service log lines in a time window."""
    for name, value in (("start_time", start_time), ("end_time", end_time), ("service", service),
                        ("level", level), ("contains", contains)):
        _check_str(name, value)
    if level:
        level = LEVEL_ALIASES.get(level.upper(), level.upper())
    rows = []
    for name in ("app_logs.jsonl", "payment_gateway_logs.jsonl",
                  "inventory_service_warning.log", "frontend_console_warning.log"):
        try:
            rows.extend(_load_jsonl(name))
        except FileNotFoundError:
            continue
    out = []
    for r in rows:
        if not _in_window(r["timestamp"], start_time, end_time):
            continue
        if service and r.get("service") != service:
            continue
        if level and r.get("level") != level:
            continue
        if contains and contains.lower() not in r.get("message", "").lower():
            continue
        out.append(r)
    out.sort(key=lambda r: r["timestamp"])
    truncated = len(out) > MAX_ROWS
    return {"count": len(out), "truncated": truncated, "rows": out[:MAX_ROWS]}


def query_traces(start_time: str | None = None, end_time: str | None = None,
                  outcome: str | None = None, request_id: str | None = None,
                  path: str | None = None) -> dict:
    """Query request traces (span breakdowns) in a time window."""
    for name, value in (("start_time", start_time), ("end_time", end_time), ("outcome", outcome),
                        ("request_id", request_id), ("path", path)):
        _check_str(name, value)
    rows = _load_jsonl("traces.jsonl")
    out = []
    for r in rows:
        if not _in_window(r["start"], start_time, end_time):
            continue
        if outcome and r.get("outcome") != outcome:
            continue
        if path and r.get("path") != path:
            continue
        if request_id and r.get("request_id") != request_id:
            continue
        out.append(r)
    out.sort(key=lambda r: r["start"])
    truncated = len(out) > MAX_ROWS
    return {"count": len(out), "truncated": truncated, "rows": out[:MAX_ROWS]}


def query_metrics(metric_name: str, start_time: str | None = None,
                   end_time: str | None = None) -> dict:
    """Query a single metric's time series in a window, downsampled to at
    most 60 points so a wide window does not flood the context."""
    for name, value in (("metric_name", metric_name), ("start_time", start_time),
                        ("end_time", end_time)):
        _check_str(name, value)
    metrics = _load_json("metrics.json")
    if metric_name not in metrics:
        return {"error": f"unknown metric '{metric_name}'", "available": sorted(metrics)}
    points = [p for p in metrics[metric_name] if _in_window(p["t"], start_time, end_time)]
    if len(points) > 60:
        step = len(points) / 60
        points = [points[int(i * step)] for i in range(60)]
    return {"metric": metric_name, "count": len(points), "points": points}


# --------------------------------------------------------------------------
# Strict tools: direct calls only, never used inside programmatic tool code
# --------------------------------------------------------------------------

def get_deployment_context() -> dict:
    """Return the deployment record, the code diff it shipped, and the
    service runbook. These are the only three static documents in the
    evidence packet."""
    meta = _load_json("deployment_metadata.json")
    with open(os.path.join(EVIDENCE_DIR, "deploy_diff.patch")) as f:
        diff = f.read()
    with open(os.path.join(EVIDENCE_DIR, "runbook.md")) as f:
        runbook = f.read()
    return {"deployment_metadata": meta, "deploy_diff": diff, "runbook": runbook}


def record_hypothesis(hypothesis: str, confidence: str, supporting_evidence: str,
                      evidence_source: str) -> dict:
    """Log a working hypothesis for the run's audit trail. It changes no
    other tool's behavior."""
    entry = {"hypothesis": hypothesis, "confidence": confidence,
             "supporting_evidence": supporting_evidence, "evidence_source": evidence_source}
    RUN_STATE["hypotheses"].append(entry)
    return {"recorded": True, "total_hypotheses_recorded": len(RUN_STATE["hypotheses"])}


# --------------------------------------------------------------------------
# Counterfactual replay: reruns sim.simulate(), the same function that
# generated the evidence packet, with only the requested changes applied.
# --------------------------------------------------------------------------

def run_counterfactual_replay(revert_retry_policy: bool, remove_gateway_burst: bool,
                              pool_capacity: int,
                              release_connection_before_gateway: bool) -> dict:
    if not isinstance(pool_capacity, int) or not 1 <= pool_capacity <= 500:
        raise ValueError("pool_capacity must be an integer between 1 and 500")
    requests = sim.simulate(
        retry_policy="previous" if revert_retry_policy else "deployed",
        gateway_burst=not remove_gateway_burst,
        pool_capacity=pool_capacity,
        release_connection_before_gateway=release_connection_before_gateway,
    )
    result = {
        "scenario": {
            "revert_retry_policy": revert_retry_policy,
            "remove_gateway_burst": remove_gateway_burst,
            "pool_capacity": pool_capacity,
            "release_connection_before_gateway": release_connection_before_gateway,
        },
        **sim.summarize(requests, pool_capacity),
    }
    RUN_STATE["replays"].append(result)
    return result
STRICT_TOOL_EXECUTORS = {
    "get_deployment_context": get_deployment_context,
    "run_counterfactual_replay": run_counterfactual_replay,
    "record_hypothesis": record_hypothesis,
}

FANOUT_TOOL_EXECUTORS = {
    "query_app_logs": query_app_logs,
    "query_traces": query_traces,
    "query_metrics": query_metrics,
}

ALL_TOOL_EXECUTORS = {**STRICT_TOOL_EXECUTORS, **FANOUT_TOOL_EXECUTORS}
