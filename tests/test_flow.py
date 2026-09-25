"""Drive run_investigation() end to end with a scripted fake client, so phase
order and request shapes are checked without calling the API."""
import copy
import json
from types import SimpleNamespace

from agent import agent
from agent.schema import INCIDENT_REPORT_SCHEMA, REPLAY_PLAN_SCHEMA
from conftest import FakeBlock, response, text_block, tool_use

CODE = "code_execution_20260120"

PLAN = {
    "leading_hypothesis": "Retries hold DB connections until the pool is exhausted",
    "hypothesis_ids": ["H01"],
    "scenarios": [{"name": "retry reverted", "purpose": "test the retry change", "revert_retry_policy": True,
                   "remove_gateway_burst": False, "pool_capacity": 15, "release_connection_before_gateway": False,
                   "expected_if_hypothesis_true": "no pool timeouts", "expected_if_hypothesis_false": "timeouts"}],
    "decision_rule": "verified if pool timeouts disappear",
}

REPORT = {
    "verdict": "verified", "trigger": "gateway 503 burst", "root_cause": "POST retries hold connections",
    "failure_mode": "503s on every endpoint", "contributing_factors": [], "ruled_out": [],
    "alternatives": [
        {"alternative": "cpu_saturation", "conclusion": "ruled_out", "reason": "flat", "evidence_ids": ["F01"]},
        {"alternative": "inventory_warning", "conclusion": "ruled_out", "reason": "low stock only",
         "evidence_ids": ["F02"]},
    ],
    "counterfactual_evidence": "R02 removes pool timeouts", "replay_ids": ["R01", "R02"],
    "documentation_evidence": "", "documentation_ids": [], "evidence_citations": ["diff"],
    "evidence_ids": ["S01", "H01"], "remaining_uncertainty": "", "recommended_fix": "revert", "confidence": "high",
}


class FakeClient:
    def __init__(self, beta_responses, report_responses):
        self.requests = []
        self._beta = list(beta_responses)
        self._reports = list(report_responses)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._beta_create))
        self.messages = SimpleNamespace(create=self._create, count_tokens=self._count_tokens)

    def _beta_create(self, **kwargs):
        self.requests.append(("beta", copy.deepcopy(kwargs)))
        return self._beta.pop(0)

    def _create(self, **kwargs):
        self.requests.append(("messages", copy.deepcopy(kwargs)))
        return self._reports.pop(0)

    def _count_tokens(self, **kwargs):
        return SimpleNamespace(input_tokens=len(kwargs["messages"][0]["content"]) // 4 + 5)


def investigation_script():
    turn1 = response([
        FakeBlock(type="thinking", thinking="Checking the deploy and CPU first.", signature="sig"),
        tool_use("t1", "get_deployment_context", {}),
        FakeBlock(type="server_tool_use", id="srvtoolu_1", name="code_execution", input={"code": "..."}),
        tool_use("t2", "query_metrics", {"metric_name": "cpu_percent"}, CODE),
        tool_use("t3", "query_app_logs", {"service": "inventory-service"}, CODE),
    ], stop_reason="tool_use")
    turn2 = response([
        FakeBlock(type="code_execution_tool_result", tool_use_id="srvtoolu_1",
                  content={"type": "code_execution_result", "stdout": "S02 cpu max 35", "stderr": "",
                           "return_code": 0}),
        tool_use("t4", "finish_investigation", {"summary": "early"}),
        tool_use("t5", "record_finding", {"claim": "CPU stays low", "source_ids": ["S02"],
                                          "alternative": "cpu_saturation"}),
        tool_use("t6", "query_traces", {"outcome": "503_pool_timeout"}),
    ], stop_reason="tool_use")
    turn3 = response([
        tool_use("t7", "record_finding", {"claim": "Low-stock notice only", "source_ids": ["S03"],
                                          "alternative": "inventory_warning"}),
        tool_use("t8", "record_hypothesis", {"hypothesis": "Retries hold connections", "confidence": "medium",
                                             "supporting_evidence": "diff", "source_ids": ["S01"]}),
        tool_use("t9", "finish_investigation", {"summary": "done"}),
    ], stop_reason="tool_use")
    return [turn1, turn2, turn3]


def run(client, **kwargs):
    events = list(agent.run_investigation(client=client, task_budget=40_000, **kwargs))
    return events, events[-1]


def test_full_run_keeps_phases_separate(runtime_assets):
    high_plan = dict(PLAN, scenarios=PLAN["scenarios"] + [dict(PLAN["scenarios"][0], name="pool 30",
                                                               revert_retry_policy=False, pool_capacity=30)])
    client = FakeClient(
        investigation_script() + [response([text_block(json.dumps(PLAN))]),
                                  response([text_block(json.dumps(high_plan))])],
        [response([text_block(json.dumps(REPORT))])],
    )
    events, record = run(client)

    kinds = [(ns, "tools" in r, "format" in r.get("output_config", {})) for ns, r in client.requests]
    assert kinds == [("beta", True, False)] * 3 + [("beta", False, True), ("messages", False, True),
                                                   ("beta", False, True)]
    phases = [e["phase"] for e in events if e["type"] == "phase"]
    assert phases == ["investigation", "plan_medium", "replay", "report", "plan_high"]

    medium_req, report_req, high_req = (client.requests[i][1] for i in (3, 4, 5))
    assert medium_req["output_config"]["format"]["schema"] == REPLAY_PLAN_SCHEMA
    assert report_req["output_config"]["format"]["schema"] == INCIDENT_REPORT_SCHEMA
    assert high_req["messages"][0]["output_config"] == {"effort": "high"}
    assert high_req["messages"][1:] == medium_req["messages"]
    assert "retry reverted" not in high_req["messages"][1]["content"]
    assert '"replays"' in report_req["messages"][0]["content"]

    assert record["stop"] == "report"
    assert record["report_accepted"] is True
    assert [r["name"] for r in record["replays"]] == ["as deployed (baseline)", "retry reverted"]
    assert record["replays"][1]["pool_exhaustion_occurred"] is False
    assert record["investigation"]["caller_rejections"] == 1
    assert record["investigation"]["stop"] == "finished"
    assert record["evidence_snapshot"]["alternatives_examined"] == ["cpu_saturation", "inventory_warning"]
    assert "replays" not in record["evidence_snapshot"]
    assert record["plan_comparison"]["high_only"][0]["pool_capacity"] == 30
    assert set(record["cost"]["by_phase"]) == {"investigation", "plan_medium", "report", "plan_high"}
    assert record["investigation"]["budget_measurement"]["total"] > record["investigation"]["budget_measurement"][
        "output_tokens"]


def test_early_finish_is_rejected_and_other_calls_still_run(runtime_assets):
    client = FakeClient(investigation_script(), [])
    events = list(agent.run_investigation(client=client, pilot=True))
    finish_results = [e for e in events if e["type"] == "tool_result" and e["name"] == "finish_investigation"]
    assert finish_results[0]["is_error"] and not finish_results[1]["is_error"]
    record = events[-1]
    assert record["stop"] == "pilot_complete"
    assert record["task_budget"] is None
    assert "task_budget" not in client.requests[0][1]["output_config"]
    assert len(client.requests) == 3
    assert record["replays"] == [] and record["replay_plans"] == {}


def test_programmatic_results_are_sent_back_alone(runtime_assets):
    client = FakeClient(investigation_script(), [])
    list(agent.run_investigation(client=client, pilot=True))
    second_request = client.requests[1][1]
    last_user = second_request["messages"][-1]
    assert last_user["role"] == "user"
    assert all(block["type"] == "tool_result" for block in last_user["content"])


def test_stop_threshold_prevents_new_requests(runtime_assets):
    client = FakeClient([], [])
    record = list(agent.run_investigation(client=client, task_budget=40_000, spent_before=2.5,
                                          stop_threshold_usd=2.5))[-1]
    assert client.requests == []
    assert record["stop"] == "cost_threshold"
    assert record["report_accepted"] is False
