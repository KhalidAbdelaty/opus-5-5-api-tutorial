"""Score recorded runs against the HarborCart ground-truth rubric and print
one summary row per run.

    python score_run.py                 # every runs/run_*.json
    python score_run.py runs/run_X.json

The checks are keyword rules over the structured report plus the run's own
replay records, so they are strict about wording. Read the report too.
"""
from __future__ import annotations

import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def text(*parts) -> str:
    out = []
    for p in parts:
        out.extend(p if isinstance(p, list) else [p])
    return " ".join(str(x) for x in out).lower()


def has_any(haystack: str, *needles: str) -> bool:
    return any(n in haystack for n in needles)


def score(record: dict) -> list[tuple[str, bool, str]]:
    r = record["report"]
    if "error" in r:
        return [("report produced", False, r["error"])]
    cause = text(r["root_cause"], r["contributing_factors"])
    ruled = text(r["ruled_out"])
    cites = r["evidence_citations"]
    replays = record.get("replays", [])

    def replay(**scenario):
        for rp in replays:
            if all(rp["scenario"].get(k) == v for k, v in scenario.items()):
                return rp
        return None

    reverted = replay(revert_retry_policy=True, remove_gateway_burst=False,
                      release_connection_before_gateway=False)
    return [
        ("retry change identified",
         has_any(cause, "allowed_methods", "retries post", "retry post", "post is retried", "retrying post",
                 "retried post", "post charges") and "retr" in cause,
         "root cause names the POST retry change"),
        ("held connection identified",
         has_any(cause, "hold", "held") and "connection" in cause,
         "root cause says the DB connection stays held through the gateway call"),
        ("amplification explained",
         has_any(cause, "pool") and has_any(cause, "exhaust", "saturat", "fill", "timeout"),
         "retries lengthen holds until the pool runs out"),
        ("trigger separated from root cause",
         "gateway" in r["trigger"].lower() and has_any(r["root_cause"].lower(), "retr", "connection"),
         "trigger is the gateway burst, root cause is internal"),
        ("red herrings rejected",
         sum(has_any(ruled, *k) for k in (("inventory",), ("frontend", "legacytrack"), ("cpu",))) >= 2,
         "at least two of inventory, frontend, CPU ruled out"),
        ("evidence cited",
         len(cites) >= 5 and has_any(text(cites), "diff", "allowed_methods")
         and has_any(text(cites), "db_pool_in_use", "metric"),
         "5+ citations including the diff and a metric"),
        ("replay consistent with verdict",
         bool(reverted) and (r["verdict"] == "verified") == (not reverted["pool_exhaustion_occurred"]),
         "reverting the retry policy was replayed and the verdict matches it"),
    ]


def summary(record: dict) -> dict:
    c = record["cost"]
    ptc_in, ptc_out = record.get("ptc_result_bytes", 0), record.get("sandbox_output_bytes", 0)
    return {
        "stop": record.get("stop"),
        "verdict": record["report"].get("verdict"),
        "confidence": record["report"].get("confidence"),
        "seconds": record["t"],
        "turns": record["turns"],
        "billed_calls": c["billed_calls"],
        "direct_tool_calls": record["direct_tool_calls"],
        "ptc_tool_calls": record["ptc_tool_calls"],
        "replays": len(record.get("replays", [])),
        "escalated_at_turn": record.get("escalated_at_turn"),
        "ptc_raw_kb": round(ptc_in / 1024, 1),
        "ptc_returned_kb": round(ptc_out / 1024, 1),
        "uncached_input": c["uncached_input_tokens"],
        "cache_read": c["cache_read_tokens"],
        "cache_write": c["cache_write_tokens"],
        "output": c["output_tokens"],
        "web_searches": c["web_searches"],
        "cost": round(c["total_cost"], 4),
    }


def main() -> None:
    paths = sys.argv[1:] or sorted(glob.glob(os.path.join(HERE, "runs", "run_*.json")))
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        checks = score(record)
        passed = sum(ok for _, ok, _ in checks)
        print(f"\n{os.path.basename(path)}: {passed}/{len(checks)} rubric checks")
        for name, ok, why in checks:
            print(f"  [{'x' if ok else ' '}] {name}: {why}")
        row = {"run": os.path.basename(path), "rubric": f"{passed}/{len(checks)}", **summary(record)}
        rows.append(row)
        print("  " + json.dumps(row))
    if rows:
        with open(os.path.join(HERE, "runs", "summary.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)


if __name__ == "__main__":
    main()
