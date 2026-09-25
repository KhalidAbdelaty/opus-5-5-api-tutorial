import json

import pytest

from agent import agent
from agent.schema import INCIDENT_REPORT_SCHEMA, REPLAY_PLAN_SCHEMA

QUERY_TOOLS = {"query_app_logs", "query_traces", "query_metrics"}
CONTROL_TOOLS = {"get_deployment_context", "record_finding", "record_hypothesis", "record_documentation",
                 "finish_investigation"}


def tools_by_name(request):
    return {t["name"]: t for t in request["tools"]}


def test_investigation_request_has_no_output_schema():
    request = agent.build_investigation_request(task_budget=50_000)
    assert "format" not in request["output_config"]
    assert request["output_config"]["effort"] == "medium"
    assert request["output_config"]["task_budget"] == {"type": "tokens", "total": 50_000}
    assert agent.BETA_TASK_BUDGETS in request["betas"]


def test_investigation_request_keeps_web_search_and_code_execution():
    tools = tools_by_name(agent.build_investigation_request(task_budget=None))
    assert tools["web_search"]["type"] == "web_search_20260318"
    assert tools["code_execution"]["type"] == "code_execution_20260120"
    assert "run_counterfactual_replay" not in tools


def test_query_tools_are_code_only_and_control_tools_direct_strict():
    tools = tools_by_name(agent.build_investigation_request(task_budget=None))
    for name in QUERY_TOOLS:
        assert tools[name]["allowed_callers"] == ["code_execution_20260120"]
        assert not tools[name].get("strict")
    for name in CONTROL_TOOLS:
        assert tools[name]["allowed_callers"] == ["direct"]
        assert tools[name]["strict"] is True


def test_pilot_request_has_no_task_budget():
    request = agent.build_investigation_request(task_budget=None)
    assert "task_budget" not in request["output_config"]
    assert agent.BETA_TASK_BUDGETS not in request["betas"]


def test_task_budget_below_minimum_is_rejected():
    with pytest.raises(ValueError):
        agent.build_investigation_request(task_budget=19_999)


def test_plan_requests_differ_only_by_per_message_effort():
    snapshot = {"sources": [], "findings": [{"id": "F01"}], "hypotheses": [], "documentation": []}
    medium = agent.build_plan_request(snapshot, "medium")
    high = agent.build_plan_request(snapshot, "high")
    for request in (medium, high):
        assert "tools" not in request
        assert request["output_config"] == {"effort": "medium",
                                            "format": {"type": "json_schema", "schema": REPLAY_PLAN_SCHEMA}}
    assert high["messages"][0] == {"role": "system", "content": [], "output_config": {"effort": "high"}}
    assert high["messages"][1:] == medium["messages"]
    assert {k: v for k, v in high.items() if k != "messages"} == {k: v for k, v in medium.items() if k != "messages"}


def test_report_request_has_schema_and_no_tools_or_web_search():
    request = agent.build_report_request({"sources": [], "replays": []})
    assert "tools" not in request
    assert "betas" not in request
    assert "web_search" not in json.dumps(request)
    assert request["output_config"]["format"] == {"type": "json_schema", "schema": INCIDENT_REPORT_SCHEMA}


def test_report_retry_does_not_end_with_assistant_prefill():
    request = agent.build_report_request({}, rejected=({"verdict": "verified"}, ["missing R-ID"]))
    assert [m["role"] for m in request["messages"]] == ["user", "assistant", "user"]
    assert "missing R-ID" in request["messages"][-1]["content"]
