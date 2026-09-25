"""Structured output schema for the final incident report.

Kept flat and free of recursion, numeric constraints, and format validators,
since Structured Outputs supports a subset of JSON Schema
(https://platform.claude.com/docs/en/build-with-claude/structured-outputs). Cost and latency are
deliberately absent: the application measures those, not the model.
"""

INCIDENT_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["verified", "inconclusive"],
            "description": "verified only if a counterfactual replay supports the root cause.",
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
        "counterfactual_evidence": {
            "type": "string",
            "description": "What the counterfactual replays showed when a suspected cause was removed.",
        },
        "documentation_evidence": {
            "type": "string",
            "description": "What library documentation says about the configuration, kept separate from incident evidence. Empty if none was consulted.",
        },
        "evidence_citations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific log lines, trace IDs, metric names, or diff lines cited as evidence.",
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
        "counterfactual_evidence",
        "documentation_evidence",
        "evidence_citations",
        "remaining_uncertainty",
        "recommended_fix",
        "confidence",
    ],
    "additionalProperties": False,
}
