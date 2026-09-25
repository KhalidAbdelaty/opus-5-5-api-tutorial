"""Run one HarborCart investigation against the live API, print each step,
and save the full run record to runs/.

    python run_investigation.py
    python run_investigation.py --no-escalation   # stay at medium effort throughout
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from agent.agent import run_investigation

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(HERE, "runs")


def short(value, limit=160) -> str:
    text = json.dumps(value) if not isinstance(value, str) else value
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def main() -> None:
    record = None
    for event in run_investigation(allow_escalation="--no-escalation" not in sys.argv):
        kind, t = event["type"], f"[{event['t']:>6.1f}s]"
        if kind == "started":
            print(f"{t} started {event['model']} at effort={event['effort']}, "
                  f"task budget {event['task_budget']:,} tokens")
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
            flag = " ERROR" if event["is_error"] else ""
            print(f"{t}          -> {event['name']} returned {event['bytes']:,} bytes{flag}")
        elif kind == "replay":
            r = event["result"]
            print(f"{t} replay {short(r['scenario'], 120)}: {r['total_503s']} 503s "
                  f"({r['read_request_503s']} on read endpoints), "
                  f"{r['pool_timeout_503s']} pool timeouts, pool saturated {r['seconds_pool_saturated']}s")
        elif kind == "hypothesis":
            e = event["entry"]
            print(f"{t} hypothesis ({e['confidence']}, {e['evidence_source']}): {short(e['hypothesis'])}")
        elif kind == "escalating_effort":
            print(f"{t} effort -> {event['to']} ({event['reason']})")
        elif kind == "nudge":
            print(f"{t} nudge: {event['text']}")
        elif kind == "done":
            record = event
            c = event["cost"]
            print(f"{t} DONE ({event['stop']}): {event['turns']} turns, "
                  f"{event['direct_tool_calls']} direct + {event['ptc_tool_calls']} PTC tool calls, "
                  f"{c['billed_calls']}/{c['api_calls']} billed calls, ${c['total_cost']:.4f}")

    if record is None:
        print("No run record produced.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(RUNS_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = "" if record["allow_escalation"] else "_medium-only"
    path = os.path.join(RUNS_DIR, f"run_{stamp}{suffix}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    print(f"\nSaved run record to {os.path.relpath(path, HERE)}")
    print("\n=== FINAL INCIDENT REPORT ===")
    print(json.dumps(record["report"], indent=2))


if __name__ == "__main__":
    main()
