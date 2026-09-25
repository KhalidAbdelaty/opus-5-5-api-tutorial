import time

import pytest

from agent.agent import MIN_TASK_BUDGET, _Limits, derive_task_budget
from agent.cost import CostLedger
from conftest import usage


def test_cost_is_accounted_per_phase():
    costs = CostLedger()
    costs.add("investigation-1", usage(input_tokens=1_000, output_tokens=500, cache_write=10_000), "investigation")
    costs.add("investigation-2", usage(input_tokens=100, output_tokens=200, cache_read=10_000), "investigation")
    costs.add("plan-medium", usage(input_tokens=5_000, output_tokens=1_000), "plan_medium")
    costs.add("report-1", usage(input_tokens=6_000, output_tokens=2_000), "report")
    data = costs.breakdown()
    assert list(data["by_phase"]) == ["investigation", "plan_medium", "report"]
    assert data["by_phase"]["investigation"]["output_tokens"] == 700
    assert data["by_phase"]["investigation"]["api_calls"] == 2
    assert sum(p["total_cost"] for p in data["by_phase"].values()) == pytest.approx(data["total_cost"])
    expected_plan = (5_000 * 4.00 + 1_000 * 20.00) / 1e6
    assert data["by_phase"]["plan_medium"]["total_cost"] == pytest.approx(expected_plan)
    assert data["cost_cache_write"] == pytest.approx(10_000 * 5.00 / 1e6)
    assert data["cost_cache_read"] == pytest.approx(10_000 * 0.20 / 1e6)


def test_stop_threshold_counts_earlier_spend_and_blocks_new_requests():
    costs = CostLedger()
    limits = _Limits(costs, spent_before=2.40, stop_threshold_usd=2.50, max_seconds=900, start=time.time())
    assert limits.blocked() is None
    costs.add("turn", usage(input_tokens=0, output_tokens=5_000), "investigation")
    assert limits.blocked() == "cost_threshold"


def test_time_limit_blocks_new_requests():
    limits = _Limits(CostLedger(), 0.0, 3.0, max_seconds=10, start=time.time() - 11)
    assert limits.blocked() == "time_limit"


def test_task_budget_is_derived_from_the_largest_pilot():
    assert derive_task_budget([31_200, 44_900, 38_000], margin=0.25, step=5_000) == 60_000
    assert derive_task_budget([1_000]) == MIN_TASK_BUDGET
    with pytest.raises(ValueError):
        derive_task_budget([])
