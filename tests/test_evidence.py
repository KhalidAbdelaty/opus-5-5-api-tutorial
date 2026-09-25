import pytest

from agent import tools
from agent.evidence import EvidenceError, EvidenceLedger

DOMAINS = ["urllib3.readthedocs.io"]


def ledger_with_sources():
    ledger = EvidenceLedger(allowed_doc_domains=DOMAINS)
    ledger.add_source("get_deployment_context", {}, tools.get_deployment_context(), "direct")
    ledger.add_source("query_metrics", {"metric_name": "cpu_percent"},
                      tools.query_metrics("cpu_percent"), "code_execution")
    ledger.add_source("query_app_logs", {"service": "inventory-service"},
                      tools.query_app_logs(service="inventory-service"), "code_execution")
    ledger.add_source("query_app_logs", {"service": "web-frontend"},
                      tools.query_app_logs(service="web-frontend"), "code_execution")
    return ledger


def complete_ledger():
    ledger = ledger_with_sources()
    ledger.record_finding("CPU stays near 30%", ["S02"], "cpu_saturation")
    ledger.record_finding("Inventory warning is a low-stock notice", ["S03"], "inventory_warning")
    ledger.record_hypothesis("Retries hold connections", "medium", "diff", ["S01"])
    ledger.add_replay("as deployed (baseline)", "baseline", {"total_503s": 124})
    ledger.add_replay("retry reverted", "test retries", {"total_503s": 93})
    return ledger


def valid_report():
    return {
        "verdict": "verified",
        "evidence_ids": ["S01", "H01"],
        "replay_ids": ["R01", "R02"],
        "documentation_ids": [],
        "alternatives": [
            {"alternative": "cpu_saturation", "conclusion": "ruled_out", "reason": "flat", "evidence_ids": ["F01"]},
            {"alternative": "inventory_warning", "conclusion": "ruled_out", "reason": "unrelated",
             "evidence_ids": ["S03"]},
        ],
    }


def test_finding_with_unknown_source_is_rejected():
    ledger = ledger_with_sources()
    with pytest.raises(EvidenceError, match="unknown source_ids"):
        ledger.record_finding("made up", ["S99"])
    with pytest.raises(EvidenceError):
        ledger.record_finding("no sources", [])
    assert ledger.findings == []


def test_hypothesis_with_unknown_source_is_rejected():
    ledger = ledger_with_sources()
    with pytest.raises(EvidenceError):
        ledger.record_hypothesis("x", "high", "y", ["S01", "S42"])


def test_alternative_must_cite_a_source_that_covers_it():
    ledger = ledger_with_sources()
    with pytest.raises(EvidenceError, match="contains evidence about cpu_saturation"):
        ledger.record_finding("CPU is fine", ["S01"], "cpu_saturation")
    with pytest.raises(EvidenceError):
        ledger.record_finding("frontend is fine", ["S03"], "frontend_warning")
    assert ledger.record_finding("frontend is fine", ["S04"], "frontend_warning") == {"recorded": "F01"}


def test_finish_requires_two_alternatives_and_a_hypothesis():
    ledger = ledger_with_sources()
    assert len(ledger.finish_problems()) == 2
    ledger.record_hypothesis("Retries hold connections", "medium", "diff", ["S01"])
    ledger.record_finding("CPU stays near 30%", ["S02"], "cpu_saturation")
    assert len(ledger.finish_problems()) == 1
    ledger.record_finding("CPU again", ["S02"], "cpu_saturation")
    assert len(ledger.finish_problems()) == 1
    ledger.record_finding("Inventory notice", ["S03"], "inventory_warning")
    assert ledger.finish_problems() == []


def test_documentation_url_must_come_from_this_runs_search():
    ledger = ledger_with_sources()
    url = "https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html"
    with pytest.raises(EvidenceError, match="not returned by web search"):
        ledger.record_documentation(url, "allowed_methods=None retries every method")
    ledger.add_retrieved_url(url + "/", "urllib3.util")
    assert ledger.record_documentation(url, "allowed_methods=None retries every method") == {"recorded": "D01"}
    ledger.add_retrieved_url("https://example.com/retry", "other")
    with pytest.raises(EvidenceError, match="allowed documentation domain"):
        ledger.record_documentation("https://example.com/retry", "x")


def test_valid_report_is_accepted():
    assert complete_ledger().validate_report(valid_report()) == []


def test_report_citing_missing_evidence_is_rejected():
    report = valid_report() | {"evidence_ids": ["S01", "F77"], "replay_ids": ["R01", "S02"]}
    problems = complete_ledger().validate_report(report)
    assert any("F77" in p for p in problems)
    assert any("R-IDs" in p for p in problems)


def test_verified_report_needs_a_replay():
    problems = complete_ledger().validate_report(valid_report() | {"replay_ids": []})
    assert any("replay" in p for p in problems)


def test_report_needs_two_examined_red_herrings():
    report = valid_report()
    report["alternatives"] = report["alternatives"][:1] + [
        {"alternative": "frontend_warning", "conclusion": "ruled_out", "reason": "x", "evidence_ids": ["S04"]}]
    problems = complete_ledger().validate_report(report)
    assert any("frontend_warning" in p and "not examined" in p for p in problems)
    assert any("at least 2" in p for p in problems)


def test_snapshot_is_frozen_before_replays():
    ledger = ledger_with_sources()
    ledger.record_hypothesis("h", "low", "e", ["S01"])
    snapshot = ledger.snapshot()
    ledger.add_replay("baseline", "b", {"total_503s": 124})
    ledger.hypotheses[0].confidence = "high"
    assert "replays" not in snapshot
    assert snapshot["hypotheses"][0]["confidence"] == "low"
