import json
import os

from agent import tools
from agent.agent import compare_plans, validate_replay_plan
from agent.evidence import EvidenceLedger

EVIDENCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evidence")


def scenario(name, **changes):
    return {"name": name, "purpose": "p", "expected_if_hypothesis_true": "t",
            "expected_if_hypothesis_false": "f", **tools.DEPLOYED_SCENARIO, **changes}


def test_replay_is_deterministic():
    first = tools.run_counterfactual_replay(True, False, 15, False)
    second = tools.run_counterfactual_replay(True, False, 15, False)
    assert first == second


def test_baseline_replay_matches_the_recorded_evidence():
    baseline = tools.run_counterfactual_replay(**tools.DEPLOYED_SCENARIO)
    with open(os.path.join(EVIDENCE, "traces.jsonl")) as f:
        outcomes = [json.loads(line)["outcome"] for line in f if line.strip()]
    assert baseline["requests_modeled"] == len(outcomes)
    assert baseline["pool_timeout_503s"] == outcomes.count("503_pool_timeout")
    assert baseline["gateway_503s"] == outcomes.count("503_gateway")


def test_replay_rejects_invalid_parameters():
    for bad in ({"pool_capacity": 0}, {"pool_capacity": True}, {"revert_retry_policy": "yes"}):
        params = dict(tools.DEPLOYED_SCENARIO) | bad
        try:
            tools.run_counterfactual_replay(**params)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


def test_plan_validation_skips_baseline_duplicates_and_bad_values():
    ledger = EvidenceLedger()
    plan = {"hypothesis_ids": ["H09"], "scenarios": [
        scenario("baseline again"),
        scenario("revert", revert_retry_policy=True),
        scenario("revert twice", revert_retry_policy=True),
        scenario("bad pool", pool_capacity=0),
        scenario("bad switch", remove_gateway_burst="yes"),
        scenario("release", release_connection_before_gateway=True),
    ]}
    runnable, problems = validate_replay_plan(plan, ledger)
    assert [s["name"] for s in runnable] == ["revert"]
    assert any("H09" in p for p in problems)
    assert any("more than" in p for p in problems)
    assert len(problems) == 6


def test_plan_comparison_is_by_scenario_parameters():
    medium = {"hypothesis_ids": ["H01"], "scenarios": [scenario("a", revert_retry_policy=True)]}
    high = {"hypothesis_ids": ["H01"], "scenarios": [scenario("renamed", revert_retry_policy=True),
                                                     scenario("b", pool_capacity=30)]}
    result = compare_plans(medium, high)
    assert len(result["shared"]) == 1
    assert result["medium_only"] == []
    assert result["high_only"][0]["pool_capacity"] == 30
    assert result["same_hypothesis_ids"]
    assert compare_plans(medium, None) is None


def test_plan_comparison_handles_several_shared_scenarios():
    scenarios = [scenario("a", revert_retry_policy=True), scenario("b", remove_gateway_burst=True),
                 scenario("c", pool_capacity=30)]
    result = compare_plans({"scenarios": scenarios}, {"scenarios": list(reversed(scenarios))})
    assert len(result["shared"]) == 3
    assert result["medium_only"] == result["high_only"] == []
