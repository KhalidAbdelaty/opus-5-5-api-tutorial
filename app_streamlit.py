"""Streamlit front end for the HarborCart incident-investigation agent.

    streamlit run app_streamlit.py

"Investigate the incident" starts a live run against the Claude Opus 5.5 API
and costs real money. "Show the latest recorded run" reopens a run saved in
runs/ by run_investigation.py or by this app, with no API call.
"""
from __future__ import annotations

import base64
import glob
import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from agent.agent import MAX_COST_USD, MAX_SECONDS, MAX_TURNS, TASK_BUDGET_TOKENS, run_investigation
from agent.cost import (MODEL, PRICE_CACHE_READ, PRICE_CACHE_WRITE_1H, PRICE_CACHE_WRITE_5M, PRICE_INPUT,
                        PRICE_OUTPUT, PRICE_WEB_SEARCH_PER_1000)

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
RUNS_DIR = HERE / "runs"
DATACAMP_URL = "https://www.datacamp.com/blog/opus-5-5"

FEATURES = [
    (":material/psychology:", "Adaptive thinking and effort",
     "Runs at medium effort and raises one later step to high only if the evidence stays ambiguous."),
    (":material/code:", "Programmatic tool calling",
     "Claude's sandboxed code queries logs, traces, and metrics, and only its summary reaches the model."),
    (":material/rule:", "Strict tools where it counts",
     "Deployment context, the hypothesis log, and the replay use strict schemas. Query tools stay open to code."),
    (":material/image_search:", "Screenshots as evidence",
     "The dashboard and architecture diagram suggest hypotheses that the metrics then have to confirm."),
    (":material/science:", "Counterfactual replay",
     "The same traffic is rerun with one change applied, to test whether the suspected cause matters."),
    (":material/data_object:", "Structured report",
     "The final answer is JSON that matches a schema, including an inconclusive verdict."),
]

st.set_page_config(page_title="HarborCart Incident Investigator", page_icon=":material/troubleshoot:",
                   layout="wide")
st.logo(str(ASSETS / "datacamp-logo.png"), link=DATACAMP_URL, size="large")

st.markdown(
    """
    <style>
      .block-container { padding-top: 2rem; max-width: 1180px; }
      .brand { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; margin-bottom: .25rem; }
      .brand img { height: 26px; }
      .brand .model { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .8rem;
        background: #F4F3EE; color: #191919; border: 1px solid #E5E2D9; border-radius: 999px;
        padding: .15rem .65rem; }
      .pill { display: inline-block; border-radius: 999px; padding: .2rem .8rem; font-weight: 700;
        font-size: .9rem; margin-right: .4rem; }
      .pill-verified { background: #E3F1E6; color: #1E5E2E; }
      .pill-inconclusive { background: #FFF1D6; color: #7A4B00; }
      .pill-high { background: #E3F1E6; color: #1E5E2E; }
      .pill-medium { background: #FFF1D6; color: #7A4B00; }
      .pill-low { background: #FBE3E0; color: #8A1F12; }
      .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: .6rem;
        margin: .6rem 0 1rem; }
      .stat { background: #F4F3EE; color: #191919; border: 1px solid #E5E2D9; border-radius: .6rem;
        padding: .55rem .8rem; }
      .stat .label { font-size: .78rem; color: #55534E; }
      .stat .value { font-size: 1.25rem; font-weight: 700; font-family: ui-monospace, Menlo, Consolas, monospace; }
    </style>
    """,
    unsafe_allow_html=True,
)


def b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


st.markdown(
    f"""
    <div class="brand">
      <img src="data:image/png;base64,{b64(ASSETS / 'claude-logo.png')}" alt="Claude logo"/>
      <span class="model">{MODEL}</span>
    </div>
    """,
    unsafe_allow_html=True,
)
st.title("HarborCart Incident Investigator")
st.markdown(
    "Claude Opus 5.5 investigates a checkout outage from logs, traces, metrics, a deployment diff, and "
    "two screenshots. It must test its root cause with a counterfactual replay before it reports."
)

# ----------------------------------------------------------------- sidebar
with st.sidebar:
    st.subheader("Run settings")
    st.markdown(
        f"- Model: `{MODEL}`\n"
        f"- Starting effort: `medium`\n"
        f"- Task budget: {TASK_BUDGET_TOKENS:,} tokens (advisory)\n"
        f"- App limits: {MAX_TURNS} turns, {MAX_SECONDS // 60} min, ${MAX_COST_USD:.2f}"
    )
    st.subheader("Price per million tokens")
    st.markdown(
        f"- Input: ${PRICE_INPUT:.2f}\n"
        f"- Cache write (5 min): ${PRICE_CACHE_WRITE_5M:.2f}\n"
        f"- Cache write (1 hour): ${PRICE_CACHE_WRITE_1H:.2f}\n"
        f"- Cache read: ${PRICE_CACHE_READ:.2f}\n"
        f"- Output: ${PRICE_OUTPUT:.2f}\n"
        f"- Web search: ${PRICE_WEB_SEARCH_PER_1000:.0f} per 1,000 searches"
    )
    if st.session_state.get("spend"):
        st.metric("Spent this session", f"${st.session_state.spend:.4f}")
    st.image(str(ASSETS / "architecture_diagram.png"), caption="Checkout path architecture", width="stretch")

# ---------------------------------------------------------------- features
st.subheader("What this run uses")
cols = st.columns(2)
for i, (icon, name, desc) in enumerate(FEATURES):
    with cols[i % 2], st.container(border=True):
        st.markdown(f"{icon} **{name}**")
        st.caption(desc)

# --------------------------------------------------------------- controls
st.write("")
left, right = st.columns([1, 1])
with left:
    live = st.button("Investigate the incident", type="primary", icon=":material/search:",
                     help="Starts a live run. Expect two to three minutes and well under one dollar.")
with right:
    recorded = st.button("Show the latest recorded run", icon=":material/history:",
                         help="Opens the newest file in runs/ without calling the API.")
st.caption("A live run calls the Claude API and is billed to the key in your .env file.")


def stats_html(items: list[tuple[str, str]]) -> str:
    cells = "".join(f"<div class='stat'><div class='label'>{html.escape(label)}</div>"
                    f"<div class='value'>{html.escape(value)}</div></div>" for label, value in items)
    return f"<div class='stats'>{cells}</div>"


def code(value, limit=140) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    text = " ".join(text.split()).replace("`", "'")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def scenario_label(s: dict) -> str:
    changes = []
    if s["revert_retry_policy"]:
        changes.append("retry policy reverted")
    if s["remove_gateway_burst"]:
        changes.append("no gateway burst")
    if s["pool_capacity"] != 15:
        changes.append(f"pool {s['pool_capacity']}")
    if s["release_connection_before_gateway"]:
        changes.append("connection released before gateway")
    return ", ".join(changes) or "as deployed"


def render_run(record: dict) -> None:
    report = record["report"]
    if "error" in report:
        st.error(f"The run ended without a valid report: {report['error']}")
        return

    verdict, confidence = report["verdict"], report["confidence"]
    st.markdown(
        f"<span class='pill pill-{verdict}'>Verdict: {verdict}</span>"
        f"<span class='pill pill-{confidence}'>Confidence: {confidence}</span>",
        unsafe_allow_html=True,
    )
    c = record["cost"]
    st.markdown(stats_html([
        ("Wall-clock time", f"{record['t']:.0f} s"),
        ("Billed API calls", str(c["billed_calls"])),
        ("Direct tool calls", str(record["direct_tool_calls"])),
        ("Calls from code", str(record["ptc_tool_calls"])),
        ("Effort", "high at the end" if record.get("escalated_at_turn") else "medium"),
        ("Cost", f"${c['total_cost']:.4f}"),
    ]), unsafe_allow_html=True)

    tabs = st.tabs([":material/description: Report", ":material/science: Replay", ":material/payments: Cost",
                    ":material/insights: Evidence", ":material/data_object: JSON"])
    with tabs[0]:
        for icon, title, key in ((":material/bolt:", "Trigger", "trigger"),
                                 (":material/my_location:", "Root cause", "root_cause"),
                                 (":material/group:", "Failure mode", "failure_mode")):
            with st.container(border=True):
                st.markdown(f"{icon} **{title}**")
                st.write(report[key])
        left, right = st.columns(2)
        with left:
            st.markdown("**Contributing factors**")
            for item in report["contributing_factors"]:
                st.markdown(f"- {item}")
            st.markdown("**Ruled out**")
            for item in report["ruled_out"]:
                st.markdown(f"- {item}")
        with right:
            st.markdown("**Recommended fix**")
            st.write(report["recommended_fix"])
            st.markdown("**Remaining uncertainty**")
            st.write(report["remaining_uncertainty"])
            st.markdown("**Documentation evidence**")
            st.write(report["documentation_evidence"] or "None consulted.")
    with tabs[1]:
        st.write(report["counterfactual_evidence"])
        if record.get("replays"):
            rows = [{
                "Scenario": scenario_label(rp["scenario"]),
                "503s": rp["total_503s"],
                "On cart and order reads": rp["read_request_503s"],
                "Pool timeouts": rp["pool_timeout_503s"],
                "Pool saturated (s)": rp["seconds_pool_saturated"],
                "Checkout p95 (s)": round(rp["p95_latency_ms_checkout_post"] / 1000, 1),
            } for rp in record["replays"]]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            st.caption("Each row is a replay the model chose to run during this investigation.")
    with tabs[2]:
        k = st.columns(4)
        k[0].metric("Uncached input", f"{c['uncached_input_tokens']:,}", f"${c['cost_uncached_input']:.4f}",
                    delta_color="off")
        k[1].metric("Cache read", f"{c['cache_read_tokens']:,}", f"${c['cost_cache_read']:.4f}", delta_color="off")
        k[2].metric("Cache write", f"{c['cache_write_tokens']:,}", f"${c['cost_cache_write']:.4f}",
                    delta_color="off")
        k[3].metric("Output", f"{c['output_tokens']:,}", f"${c['cost_output']:.4f}", delta_color="off")
        if record.get("calls"):
            calls = pd.DataFrame(record["calls"])
            calls = calls[(calls[["input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"]]
                           .sum(axis=1)) > 0]
            st.dataframe(calls[["turn", "effort", "stop_reason", "input_tokens", "cache_read_tokens",
                                "cache_write_tokens", "output_tokens", "cost"]],
                         hide_index=True, width="stretch")
            st.caption("Calls that only resumed paused code execution report zero usage and are hidden.")
    with tabs[3]:
        st.image(str(ASSETS / "monitoring_dashboard.png"), width="stretch")
        st.markdown("**Hypothesis trail**")
        for h in record.get("hypotheses", []):
            st.markdown(f"- *{h['confidence']}, from {h['evidence_source']}*: {h['hypothesis']}")
        st.markdown("**Evidence citations**")
        for item in report["evidence_citations"]:
            st.markdown(f"- {item}")
    with tabs[4]:
        st.json(report)


def latest_run() -> dict | None:
    paths = sorted(glob.glob(str(RUNS_DIR / "run_*.json")))
    if not paths:
        return None
    with open(paths[-1], encoding="utf-8") as f:
        return json.load(f)


if live:
    counters = {"turns": 0, "direct": 0, "ptc": 0, "effort": "medium", "cost": 0.0, "t": 0.0}
    stats_slot = st.empty()

    def show_counters():
        stats_slot.markdown(stats_html([
            ("Elapsed", f"{counters['t']:.0f} s"),
            ("API calls", str(counters["turns"])),
            ("Direct tool calls", str(counters["direct"])),
            ("Calls from code", str(counters["ptc"])),
            ("Effort", counters["effort"]),
            ("Running cost", f"${counters['cost']:.4f}"),
        ]), unsafe_allow_html=True)

    show_counters()
    record = None
    with st.status("Investigating the HarborCart checkout outage...", expanded=True) as status:
        log = st.container(height=420, border=True)
        for event in run_investigation():
            kind = event["type"]
            counters["t"] = event["t"]
            stamp = f"`{event['t']:>5.1f}s`"
            if kind == "usage":
                counters["turns"] = event["turn"]
                counters["cost"] = event["running_total"]
            elif kind == "progress":
                log.markdown(f"{stamp} :material/chat: {code(event['text'], 300)}")
            elif kind == "tool_call":
                counters["ptc" if event["via_ptc"] else "direct"] += 1
                via = ":orange-badge[code]" if event["via_ptc"] else ":blue-badge[direct]"
                log.markdown(f"{stamp} {via} `{event['name']}` `{code(event['input'])}`")
            elif kind == "tool_result" and event["is_error"]:
                log.markdown(f"{stamp} :red-badge[error] `{event['name']}` returned an error")
            elif kind == "hypothesis":
                e = event["entry"]
                log.markdown(f"{stamp} :material/lightbulb: **{e['confidence']}** ({e['evidence_source']}): "
                             f"{code(e['hypothesis'], 260)}")
            elif kind == "replay":
                r = event["result"]
                log.markdown(f"{stamp} :material/science: replay `{code(r['scenario'])}` -> "
                             f"**{r['total_503s']}** 503s, {r['read_request_503s']} on read endpoints, "
                             f"pool saturated {r['seconds_pool_saturated']} s")
            elif kind == "escalating_effort":
                counters["effort"] = "high"
                log.markdown(f"{stamp} :material/trending_up: effort raised to **high**: {event['reason']}")
                st.toast("Effort raised to high for the next step")
            elif kind == "nudge":
                log.markdown(f"{stamp} :material/info: asked the model to run a replay before reporting")
            elif kind == "done":
                record = event
                status.update(label=f"Investigation finished ({event['stop']})", state="complete",
                              expanded=False)
            show_counters()

    if record is not None:
        RUNS_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        with open(RUNS_DIR / f"run_{stamp}.json", "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        st.session_state.spend = st.session_state.get("spend", 0.0) + record["cost"]["total_cost"]
        st.session_state.record = record

if recorded:
    st.session_state.record = latest_run()
    if st.session_state.record is None:
        st.info("No recorded runs yet. Run `python run_investigation.py` or start a live run first.")

if st.session_state.get("record"):
    st.divider()
    render_run(st.session_state.record)
