"""Score recorded runs against the HarborCart ground-truth rubric and print
one summary row per run.

    python score_run.py                        # every runs/measured/run_*.json
    python score_run.py runs/measured/run_X.json

The checks are keyword rules over the structured report plus the run's own
replay records, so they are strict about wording. Read the report too. The
ground truth lives here and in build_evidence.py; the agent never sees it.
"""
from __future__ import annotations

import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RETRY_REVERTED = {"revert_retry_policy": True, "remove_gateway_burst": False, "pool_capacity": 15,
                  "release_connection_before_gateway": False}


def text(*parts) -> str:
    out = []
    for p in parts:
        out.extend(p if isinstance(p, list) else [p])
    return " ".join(str(x) for x in out).lower()


def has_any(haystack: str, *needles: str) -> bool:
    return any(n in haystack for n in needles)


def plan_tests_retry_revert(plan: dict | None) -> bool:
    return any(all(s.get(k) == v for k, v in RETRY_REVERTED.items()) for s in (plan or {}).get("scenarios", []))


def score(record: dict) -> list[tuple[str, bool, str]]:
    r = record["report"]
    if "error" in r:
        return [("report accepted", False, r["error"])]
    cause = text(r["root_cause"], r["contributing_factors"])
    cites = r["evidence_citations"]
    replays = record.get("replays", [])
    ruled_out = {a["alternative"] for a in r.get("alternatives", []) if a["conclusion"] == "ruled_out"}
    reverted = next((rp for rp in replays if all(rp["scenario"].get(k) == v for k, v in RETRY_REVERTED.items())),
                    None)
    return [
        ("report accepted", record.get("report_accepted", False), "evidence IDs and red-herring rule passed"),
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
         len(ruled_out) >= 2,
         f"at least two of the three red herrings ruled out (got {sorted(ruled_out)})"),
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
    inv = record["investigation"]
    plans = record.get("replay_plans", {})
    by_phase = {p: round(v["total_cost"], 4) for p, v in c["by_phase"].items()}
    return {
        "stop": record.get("stop"),
        "verdict": record["report"].get("verdict"),
        "confidence": record["report"].get("confidence"),
        "seconds": record["t"],
        "task_budget": record.get("task_budget"),
        "budget_measured": inv["budget_measurement"]["total"],
        "investigation_turns": inv["turns"],
        "direct_tool_calls": inv["direct_tool_calls"],
        "ptc_tool_calls": inv["ptc_tool_calls"],
        "caller_rejections": inv["caller_rejections"],
        "ptc_raw_kb": round(inv["ptc_result_bytes"] / 1024, 1),
        "ptc_returned_kb": round(inv["sandbox_output_bytes"] / 1024, 1),
        "sources": len(record["evidence_ledger"]["sources"]),
        "findings": len(record["evidence_ledger"]["findings"]),
        "alternatives_examined": record["evidence_ledger"]["alternatives_examined"],
        "replays": len(record.get("replays", [])),
        "report_attempts": record.get("report_attempts"),
        "medium_plan_scenarios": len(((plans.get("medium") or {}).get("plan") or {}).get("scenarios", [])),
        "high_plan_scenarios": len(((plans.get("high") or {}).get("plan") or {}).get("scenarios", [])),
        "medium_plan_tests_retry_revert": plan_tests_retry_revert((plans.get("medium") or {}).get("plan")),
        "high_plan_tests_retry_revert": plan_tests_retry_revert((plans.get("high") or {}).get("plan")),
        "medium_plan_output_tokens": (plans.get("medium") or {}).get("output_tokens"),
        "high_plan_output_tokens": (plans.get("high") or {}).get("output_tokens"),
        "cache_read": c["cache_read_tokens"],
        "cache_write": c["cache_write_tokens"],
        "uncached_input": c["uncached_input_tokens"],
        "output": c["output_tokens"],
        "web_searches": c["web_searches"],
        "cost": round(c["total_cost"], 4),
        "cost_by_phase": by_phase,
    }


def main() -> None:
    paths = sys.argv[1:] or sorted(glob.glob(os.path.join(HERE, "runs", "measured", "run_*.json")))
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
        out = os.path.join(os.path.dirname(os.path.abspath(paths[0])), "summary.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        print(f"\nSaved {os.path.relpath(out, HERE)}")


if __name__ == "__main__":
    main()
