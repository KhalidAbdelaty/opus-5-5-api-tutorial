"""Run one HarborCart investigation against the live API, print each step,
and save the full run record.

    python run_investigation.py --task-budget 60000
    python run_investigation.py --pilot              # investigation only, no task budget
    python run_investigation.py --stop-threshold 0.75 --out runs/measured
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from agent.agent import STOP_THRESHOLD_USD, TASK_BUDGET_TOKENS, run_investigation

HERE = os.path.dirname(os.path.abspath(__file__))


def short(value, limit=160) -> str:
    text = json.dumps(value) if not isinstance(value, str) else value
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def print_event(event: dict) -> None:
    kind, t = event["type"], f"[{event['t']:>6.1f}s]"
    if kind == "started":
        budget = "none (pilot)" if event["task_budget"] is None else f"{event['task_budget']:,} tokens"
        print(f"{t} started {event['model']} at effort={event['effort']}, task budget {budget}")
    elif kind == "phase":
        print(f"{t} --- phase: {event['phase']} ---")
    elif kind == "usage":
        print(f"{t} turn {event['turn']}: {event['uncached_input']} uncached in, "
              f"{event['cache_read']} cache read, {event['cache_write']} cache write, "
              f"{event['output']} out -> ${event['cost']:.4f} (total ${event['running_total']:.4f})")
    elif kind == "progress":
        print(f"{t} progress: {short(event['text'])}")
    elif kind == "tool_call":
        via = "PTC   " if event["via_ptc"] else "direct"
        print(f"{t} [{via}] {event['name']}({short(event['input'], 110)})")
    elif kind == "tool_result":
        tag = f" {event['source_id']}" if event.get("source_id") else ""
        tag += f" {event['recorded']}" if event.get("recorded") else ""
        flag = f" ERROR: {short(event['error'], 140)}" if event["is_error"] else ""
        print(f"{t}          -> {event['name']} returned {event['bytes']:,} bytes{tag}{flag}")
    elif kind in ("finding", "hypothesis", "documentation"):
        e = event["entry"]
        text = e.get("claim") or e.get("hypothesis")
        extra = f" [{e['alternative']}]" if e.get("alternative", "none") != "none" else ""
        extra += f" ({e['confidence']})" if "confidence" in e else ""
        print(f"{t} {kind} {event['id']}{extra}: {short(text)}")
    elif kind == "snapshot":
        print(f"{t} evidence snapshot ({event['stop']}): {event['sources']} sources, {event['findings']} findings, "
              f"{event['hypotheses']} hypotheses, {event['documentation']} docs, "
              f"alternatives examined {event['alternatives_examined']}")
    elif kind == "plan":
        plan = event["plan"] or {}
        names = [s["name"] for s in plan.get("scenarios", [])]
        print(f"{t} {event['effort']} replay plan: {len(names)} scenarios {names} "
              f"({event['output_tokens']} out, ${event['cost']:.4f})")
    elif kind == "replay":
        r = event["result"]
        print(f"{t} replay {event['id']} {event['name']}: {r['total_503s']} 503s "
              f"({r['read_request_503s']} on read endpoints), {r['pool_timeout_503s']} pool timeouts, "
              f"pool saturated {r['seconds_pool_saturated']}s")
    elif kind == "report_check":
        state = "accepted" if event["accepted"] else f"rejected: {event['problems']}"
        print(f"{t} report attempt {event['attempt']} {state}")
    elif kind == "nudge":
        print(f"{t} nudge: {event['text']}")
    elif kind == "done":
        c = event["cost"]
        inv = event["investigation"]
        print(f"{t} DONE ({event['stop']}): {inv['turns']} investigation turns, "
              f"{inv['direct_tool_calls']} direct + {inv['ptc_tool_calls']} PTC tool calls, "
              f"{c['billed_calls']}/{c['api_calls']} billed calls, ${c['total_cost']:.4f}")
        for phase, p in c["by_phase"].items():
            print(f"           {phase}: {p['output_tokens']} out, ${p['total_cost']:.4f}")
        m = inv["budget_measurement"]
        print(f"           budget measurement: {m['output_tokens']} output + {m['tool_result_tokens']} "
              f"tool-result tokens = {m['total']}")


def save_record(record: dict, out_dir: str, label: str = "") -> str:
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    kind = "pilot" if record["pilot"] else "run"
    path = os.path.join(out_dir, f"{kind}_{stamp}{label}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pilot", action="store_true", help="investigation phase only, without a task budget")
    parser.add_argument("--task-budget", type=int, default=TASK_BUDGET_TOKENS)
    parser.add_argument("--stop-threshold", type=float, default=STOP_THRESHOLD_USD,
                        help="launch no new request once this much has been spent (not a hard cap)")
    parser.add_argument("--no-high-plan", action="store_true", help="skip the high-effort plan evaluation")
    parser.add_argument("--out", default=os.path.join(HERE, "runs"))
    args = parser.parse_args()

    record = None
    for event in run_investigation(task_budget=args.task_budget, pilot=args.pilot,
                                   stop_threshold_usd=args.stop_threshold,
                                   evaluate_high_plan=not args.no_high_plan):
        print_event(event)
        if event["type"] == "done":
            record = event

    if record is None:
        print("No run record produced.", file=sys.stderr)
        sys.exit(1)
    path = save_record(record, args.out)
    print(f"\nSaved run record to {os.path.relpath(path, HERE)}")
    if not record["pilot"]:
        print("\n=== FINAL INCIDENT REPORT ===")
        print(json.dumps(record["report"], indent=2))


if __name__ == "__main__":
    main()
