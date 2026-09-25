"""Structured output schemas for the replay-plan and reporting requests.

Both are used only in requests without tools or web search: structured
outputs cannot be combined with citations, and web search results always
carry citations. Kept flat and free of recursion, numeric constraints, and
format validators, since Structured Outputs supports a subset of JSON Schema
(https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
The application checks ranges and evidence IDs after parsing. Cost and
latency are deliberately absent: the application measures those, not the
model.
"""
from .evidence import RED_HERRINGS

MAX_REPLAY_SCENARIOS = 5

REPLAY_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "leading_hypothesis": {
            "type": "string",
            "description": "The root-cause hypothesis the replays should test.",
        },
        "hypothesis_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "H-IDs from the evidence snapshot that the plan tests.",
        },
        "scenarios": {
            "type": "array",
            "description": (
                f"At most {MAX_REPLAY_SCENARIOS} replays. The as-deployed baseline runs automatically, "
                "so do not include it."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "purpose": {"type": "string", "description": "Which hypothesis this replay can confirm or refute."},
                    "revert_retry_policy": {"type": "boolean"},
                    "remove_gateway_burst": {"type": "boolean"},
                    "pool_capacity": {"type": "integer", "description": "DB connections per instance, 1 to 500. Deployed: 15."},
                    "release_connection_before_gateway": {"type": "boolean"},
                    "expected_if_hypothesis_true": {"type": "string"},
                    "expected_if_hypothesis_false": {"type": "string"},
                },
                "required": ["name", "purpose", "revert_retry_policy", "remove_gateway_burst", "pool_capacity",
                             "release_connection_before_gateway", "expected_if_hypothesis_true",
                             "expected_if_hypothesis_false"],
                "additionalProperties": False,
            },
        },
        "decision_rule": {
            "type": "string",
            "description": "How the replay outputs decide between verified and inconclusive.",
        },
    },
    "required": ["leading_hypothesis", "hypothesis_ids", "scenarios", "decision_rule"],
    "additionalProperties": False,
}

INCIDENT_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["verified", "inconclusive"],
            "description": "verified only if the replay outputs support the root cause.",
        },
        "trigger": {
            "type": "string",
            "description": "The external event that started the incident.",
        },
        "root_cause": {
            "type": "string",
            "description": "The underlying condition that turned the trigger into a customer-facing outage.",
        },
        "failure_mode": {
            "type": "string",
            "description": "What customers experienced.",
        },
        "contributing_factors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Conditions that made the incident worse but were not the root cause.",
        },
        "ruled_out": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Hypotheses considered and rejected, each with the evidence that rejected it.",
        },
        "alternatives": {
            "type": "array",
            "description": "Each alternative explanation examined in the ledger and what the evidence showed.",
            "items": {
                "type": "object",
                "properties": {
                    "alternative": {"type": "string", "enum": sorted(RED_HERRINGS)},
                    "conclusion": {"type": "string", "enum": ["ruled_out", "contributing", "undetermined"]},
                    "reason": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["alternative", "conclusion", "reason", "evidence_ids"],
                "additionalProperties": False,
            },
        },
        "counterfactual_evidence": {
            "type": "string",
            "description": "What the replays showed when a suspected cause was removed.",
        },
        "replay_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "R-IDs of the replay outputs the verdict rests on.",
        },
        "documentation_evidence": {
            "type": "string",
            "description": "What library documentation says about the configuration, kept separate from incident evidence. Empty if none was consulted.",
        },
        "documentation_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "D-IDs of documentation entries used. Empty if none.",
        },
        "evidence_citations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific log lines, trace IDs, metric names, or diff lines cited as evidence.",
        },
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ledger IDs (S, F, H) behind the trigger, root cause, and failure mode.",
        },
        "remaining_uncertainty": {
            "type": "string",
            "description": "What the evidence does not settle.",
        },
        "recommended_fix": {
            "type": "string",
            "description": "The concrete change recommended to prevent recurrence.",
        },
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
        },
    },
    "required": [
        "verdict",
        "trigger",
        "root_cause",
        "failure_mode",
        "contributing_factors",
        "ruled_out",
        "alternatives",
        "counterfactual_evidence",
        "replay_ids",
        "documentation_evidence",
        "documentation_ids",
        "evidence_citations",
        "evidence_ids",
        "remaining_uncertainty",
        "recommended_fix",
        "confidence",
    ],
    "additionalProperties": False,
}
