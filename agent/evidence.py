"""Evidence ledger for one HarborCart investigation.

Every evidence tool result gets a source ID (S01, S02, ...) before Claude sees
it. Findings (F), hypotheses (H), and documentation notes (D) are accepted
only if they cite source IDs that exist, and documentation URLs only if web
search actually returned them in this run. Replay outputs (R) are added by the
application, never by the model.

The ledger checks provenance, not truth: a finding that cites S04 is tied to
a real query result, but the claim itself is still Claude's reading of it.

Red herrings. The fixture seeds three explanations that do not cause the
incident (RED_HERRINGS). The model only sees them as "alternatives" to
examine. The investigation cannot finish, and the report is not accepted,
until at least MIN_RED_HERRINGS_CHECKED of them have been examined against a
source that actually covers them.
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

RED_HERRINGS = {
    "inventory_warning": "inventory-service low-stock warning (SKU HC-2291 below reorder threshold)",
    "frontend_warning": "web-frontend warning about the deprecated legacyTrack() analytics snippet",
    "cpu_saturation": "host CPU saturation on checkout-api-3",
}
MIN_RED_HERRINGS_CHECKED = 2
NO_ALTERNATIVE = "none"


class EvidenceError(ValueError):
    """A ledger entry that cites evidence the run never produced."""


def _covers(source: "Source", alternative: str) -> bool:
    if alternative == "inventory_warning":
        return source.services.get("inventory-service", 0) > 0
    if alternative == "frontend_warning":
        return source.services.get("web-frontend", 0) > 0
    if alternative == "cpu_saturation":
        return source.tool == "query_metrics" and source.metric == "cpu_percent" and source.row_count > 0
    return False


def _normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}"


@dataclass
class Source:
    id: str
    tool: str
    args: dict
    caller: str
    row_count: int
    result_bytes: int
    services: dict = field(default_factory=dict)
    metric: str | None = None


@dataclass
class Finding:
    id: str
    claim: str
    source_ids: list[str]
    alternative: str


@dataclass
class Hypothesis:
    id: str
    hypothesis: str
    confidence: str
    supporting_evidence: str
    source_ids: list[str]


@dataclass
class Documentation:
    id: str
    url: str
    claim: str


@dataclass
class ReplayOutput:
    id: str
    scenario_name: str
    purpose: str
    result: dict


class EvidenceLedger:
    def __init__(self, allowed_doc_domains: list[str] | None = None):
        self.allowed_doc_domains = [d.lower() for d in (allowed_doc_domains or [])]
        self.sources: dict[str, Source] = {}
        self.findings: list[Finding] = []
        self.hypotheses: list[Hypothesis] = []
        self.documentation: list[Documentation] = []
        self.replays: list[ReplayOutput] = []
        self.retrieved_urls: dict[str, dict] = {}

    # ------------------------------------------------------------ sources
    def add_source(self, tool: str, args: dict, result: dict, caller: str) -> str:
        source_id = f"S{len(self.sources) + 1:02d}"
        rows = result.get("rows") or result.get("points") or []
        self.sources[source_id] = Source(
            id=source_id, tool=tool, args=dict(args or {}), caller=caller,
            row_count=int(result.get("count", len(rows))),
            result_bytes=len(json.dumps(result)),
            services=dict(result.get("services", {})),
            metric=result.get("metric"),
        )
        return source_id

    def add_retrieved_url(self, url: str, title: str = "") -> None:
        if url:
            self.retrieved_urls[_normalize_url(url)] = {"url": url, "title": title}

    def _check_source_ids(self, source_ids: list[str]) -> None:
        if not source_ids:
            raise EvidenceError("cite at least one source_id")
        missing = [s for s in source_ids if s not in self.sources]
        if missing:
            raise EvidenceError(f"unknown source_ids {missing}; known: {sorted(self.sources)}")

    # ------------------------------------------------------------ model-recorded entries
    def record_finding(self, claim: str, source_ids: list[str], alternative: str = NO_ALTERNATIVE) -> dict:
        self._check_source_ids(source_ids)
        if alternative != NO_ALTERNATIVE:
            if alternative not in RED_HERRINGS:
                raise EvidenceError(f"unknown alternative '{alternative}'")
            if not any(_covers(self.sources[s], alternative) for s in source_ids):
                raise EvidenceError(
                    f"none of {source_ids} contains evidence about {alternative}; cite the query "
                    f"result that covers it")
        finding = Finding(f"F{len(self.findings) + 1:02d}", claim, list(source_ids), alternative)
        self.findings.append(finding)
        return {"recorded": finding.id}

    def record_hypothesis(self, hypothesis: str, confidence: str, supporting_evidence: str,
                          source_ids: list[str]) -> dict:
        if confidence not in ("low", "medium", "high"):
            raise EvidenceError("confidence must be low, medium, or high")
        self._check_source_ids(source_ids)
        entry = Hypothesis(f"H{len(self.hypotheses) + 1:02d}", hypothesis, confidence,
                           supporting_evidence, list(source_ids))
        self.hypotheses.append(entry)
        return {"recorded": entry.id}

    def record_documentation(self, url: str, claim: str) -> dict:
        key = _normalize_url(url)
        domain = urlsplit(url.strip()).netloc.lower()
        if self.allowed_doc_domains and not any(domain == d or domain.endswith("." + d)
                                                for d in self.allowed_doc_domains):
            raise EvidenceError(f"{domain or url!r} is not an allowed documentation domain")
        if key not in self.retrieved_urls:
            raise EvidenceError("this URL was not returned by web search in this run")
        doc = Documentation(f"D{len(self.documentation) + 1:02d}", self.retrieved_urls[key]["url"], claim)
        self.documentation.append(doc)
        return {"recorded": doc.id}

    # ------------------------------------------------------------ replays
    def add_replay(self, scenario_name: str, purpose: str, result: dict) -> str:
        replay = ReplayOutput(f"R{len(self.replays) + 1:02d}", scenario_name, purpose, result)
        self.replays.append(replay)
        return replay.id

    # ------------------------------------------------------------ gates
    def alternatives_checked(self) -> list[str]:
        return sorted({f.alternative for f in self.findings if f.alternative != NO_ALTERNATIVE})

    def finish_problems(self) -> list[str]:
        problems = []
        if not self.hypotheses:
            problems.append("record at least one hypothesis with record_hypothesis")
        checked = self.alternatives_checked()
        if len(checked) < MIN_RED_HERRINGS_CHECKED:
            remaining = [k for k in RED_HERRINGS if k not in checked]
            problems.append(
                f"examine at least {MIN_RED_HERRINGS_CHECKED} alternative explanations with record_finding "
                f"(examined so far: {checked or 'none'}; not yet examined: {remaining})")
        return problems

    def all_ids(self) -> set[str]:
        return (set(self.sources) | {f.id for f in self.findings} | {h.id for h in self.hypotheses}
                | {d.id for d in self.documentation} | {r.id for r in self.replays})

    def validate_report(self, report: dict) -> list[str]:
        """Problems that stop the report from being accepted. Empty means accepted."""
        problems = []
        known = self.all_ids()

        def check_ids(label: str, ids: list[str], prefix: str | None = None) -> None:
            for i in ids:
                if i not in known:
                    problems.append(f"{label} cites unknown evidence ID {i}")
                elif prefix and not i.startswith(prefix):
                    problems.append(f"{label} must cite {prefix}-IDs, got {i}")

        check_ids("evidence_ids", report.get("evidence_ids", []))
        check_ids("replay_ids", report.get("replay_ids", []), "R")
        check_ids("documentation_ids", report.get("documentation_ids", []), "D")

        if report.get("verdict") == "verified" and not report.get("replay_ids"):
            problems.append("a verified verdict must cite at least one replay output (R-ID)")

        checked = set(self.alternatives_checked())
        reported = set()
        for item in report.get("alternatives", []):
            name = item.get("alternative")
            check_ids(f"alternatives[{name}]", item.get("evidence_ids", []))
            if name not in checked:
                problems.append(f"alternative {name} is in the report but was not examined in the ledger")
            else:
                reported.add(name)
        if len(reported) < MIN_RED_HERRINGS_CHECKED:
            problems.append(f"the report must address at least {MIN_RED_HERRINGS_CHECKED} examined alternatives")
        return problems

    # ------------------------------------------------------------ views
    def snapshot(self) -> dict:
        """Frozen copy of the evidence gathered so far, taken before any replay."""
        return copy.deepcopy({
            "sources": [asdict(s) for s in self.sources.values()],
            "findings": [asdict(f) for f in self.findings],
            "hypotheses": [asdict(h) for h in self.hypotheses],
            "documentation": [asdict(d) for d in self.documentation],
            "alternatives_examined": self.alternatives_checked(),
        })

    def to_dict(self) -> dict:
        data = self.snapshot()
        data["replays"] = [asdict(r) for r in self.replays]
        data["retrieved_urls"] = sorted(v["url"] for v in self.retrieved_urls.values())
        return data
