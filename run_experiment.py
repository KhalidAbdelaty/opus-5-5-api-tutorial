"""Pilot-then-measure experiment for the HarborCart investigator.

    python run_experiment.py pilots      # 3 investigations without a task budget
    python run_experiment.py measured    # 3 full runs with the budget derived from the pilots

Pilots run the investigation phase only and measure what the task budget
counts: output tokens plus the tool-result tokens Claude saw. The budget is
the largest pilot measurement plus BUDGET_MARGIN, rounded up to BUDGET_STEP.

Spend across both steps is checked against STOP_THRESHOLD_USD before every
new run and every new request. It is a stop-launching threshold, not a hard
cap: a request already in flight can still take the total past it.
"""
from __future__ import annotations

import glob
import json
import os
import sys

from agent.agent import derive_task_budget, run_investigation
from run_investigation import HERE, print_event, save_record

PILOT_DIR = os.path.join(HERE, "runs", "pilot")
MEASURED_DIR = os.path.join(HERE, "runs", "measured")
BUDGET_FILE = os.path.join(PILOT_DIR, "budget.json")
ADJUSTMENTS_FILE = os.path.join(HERE, "runs", "spend_adjustments.json")
RUNS_PER_STEP = 3
BUDGET_MARGIN = 0.25
BUDGET_STEP = 5_000
STOP_THRESHOLD_USD = 2.50
FIRST_RUN_ESTIMATE_USD = 0.60


def load(pattern: str) -> list[dict]:
    records = []
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8") as f:
            records.append(json.load(f) | {"_path": os.path.relpath(path, HERE)})
    return records


def spend_adjustments() -> list[dict]:
    """API spend that no saved run record contains, such as a run that crashed."""
    if not os.path.exists(ADJUSTMENTS_FILE):
        return []
    with open(ADJUSTMENTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def spent_so_far() -> float:
    records = load(os.path.join(PILOT_DIR, "pilot_*.json")) + load(os.path.join(MEASURED_DIR, "run_*.json"))
    return sum(r["cost"]["total_cost"] for r in records) + sum(a["usd"] for a in spend_adjustments())


def run_step(pilot: bool, task_budget: int | None, out_dir: str) -> None:
    existing = load(os.path.join(out_dir, "pilot_*.json" if pilot else "run_*.json"))
    costs = [r["cost"]["total_cost"] for r in existing]
    for i in range(len(existing), RUNS_PER_STEP):
        spent = spent_so_far()
        estimate = max(costs, default=FIRST_RUN_ESTIMATE_USD)
        if spent + estimate > STOP_THRESHOLD_USD:
            print(f"Stopping before run {i + 1}: ${spent:.4f} spent, next run estimated at ${estimate:.4f}, "
                  f"threshold ${STOP_THRESHOLD_USD:.2f}.")
            return
        print(f"\n===== {'pilot' if pilot else 'measured run'} {i + 1}/{RUNS_PER_STEP} "
              f"(spent so far ${spent:.4f}) =====")
        record = None
        try:
            for event in run_investigation(task_budget=task_budget, pilot=pilot,
                                           stop_threshold_usd=STOP_THRESHOLD_USD, spent_before=spent):
                print_event(event)
                if event["type"] == "done":
                    record = event
        except Exception:
            print(f"Run {i + 1} crashed. Its spend is not in any record: add it to "
                  f"{os.path.relpath(ADJUSTMENTS_FILE, HERE)} before running again.")
            raise
        path = save_record(record, out_dir)
        costs.append(record["cost"]["total_cost"])
        print(f"Saved {os.path.relpath(path, HERE)}")


def pilots() -> None:
    run_step(pilot=True, task_budget=None, out_dir=PILOT_DIR)
    records = load(os.path.join(PILOT_DIR, "pilot_*.json"))
    measured = [r for r in records if r["investigation"]["stop"] == "finished"
                and r["investigation"]["budget_measurement"]["total"] is not None]
    if not measured:
        print("No finished pilot with a budget measurement; budget not derived.")
        return
    spends = [r["investigation"]["budget_measurement"]["total"] for r in measured]
    budget = derive_task_budget(spends, BUDGET_MARGIN, BUDGET_STEP)
    summary = {
        "pilots": [{"file": r["_path"], "stop": r["investigation"]["stop"],
                    **r["investigation"]["budget_measurement"], "cost": r["cost"]["total_cost"]} for r in records],
        "pilots_used": len(measured),
        "largest_spend": max(spends),
        "margin": BUDGET_MARGIN,
        "rounded_up_to": BUDGET_STEP,
        "task_budget": budget,
    }
    with open(BUDGET_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\nPilot spends {spends}; largest {max(spends):,} x {1 + BUDGET_MARGIN} -> task budget {budget:,} tokens")
    print(f"Saved {os.path.relpath(BUDGET_FILE, HERE)}")


def measured() -> None:
    if not os.path.exists(BUDGET_FILE):
        sys.exit("Run the pilots first: python run_experiment.py pilots")
    with open(BUDGET_FILE, encoding="utf-8") as f:
        budget = json.load(f)["task_budget"]
    run_step(pilot=False, task_budget=budget, out_dir=MEASURED_DIR)
    print(f"\nTotal spent across pilots and measured runs: ${spent_so_far():.4f}")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else ""
    if step == "pilots":
        pilots()
    elif step == "measured":
        measured()
    else:
        sys.exit(__doc__)
