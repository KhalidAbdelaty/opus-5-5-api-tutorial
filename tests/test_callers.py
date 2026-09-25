from agent import tools
from agent.agent import execute_tool_call
from agent.evidence import EvidenceLedger

CODE = "code_execution_20260120"


def test_query_tool_called_directly_is_rejected_without_running():
    ledger = EvidenceLedger()
    result, is_error = execute_tool_call(ledger, "query_metrics", {"metric_name": "cpu_percent"}, "direct")
    assert is_error
    assert "code execution" in result["error"]
    assert ledger.sources == {}


def test_query_tool_from_code_execution_runs_and_gets_a_source_id():
    ledger = EvidenceLedger()
    result, is_error = execute_tool_call(ledger, "query_metrics", {"metric_name": "cpu_percent"}, CODE)
    assert not is_error
    assert result["source_id"] == "S01"
    assert ledger.sources["S01"].caller == tools.CODE_EXECUTION


def test_control_tool_from_code_execution_is_rejected():
    ledger = EvidenceLedger()
    result, is_error = execute_tool_call(ledger, "get_deployment_context", {}, CODE)
    assert is_error
    assert "directly" in result["error"]
    result, is_error = execute_tool_call(ledger, "finish_investigation", {"summary": "done"}, CODE)
    assert is_error


def test_newer_code_execution_caller_version_counts_as_code():
    assert tools.check_caller("query_traces", "code_execution_20260521") is None


def test_unknown_tool_is_rejected():
    result, is_error = execute_tool_call(EvidenceLedger(), "run_counterfactual_replay",
                                         {"revert_retry_policy": True}, "direct")
    assert is_error
    assert "unknown tool" in result["error"]


def test_bad_programmatic_arguments_become_tool_errors():
    ledger = EvidenceLedger()
    result, is_error = execute_tool_call(ledger, "query_app_logs", {"start_time": "yesterday"}, CODE)
    assert is_error and "ISO 8601" in result["error"]
    result, is_error = execute_tool_call(ledger, "query_app_logs", {"not_a_field": 1}, CODE)
    assert is_error
    assert ledger.sources == {}
