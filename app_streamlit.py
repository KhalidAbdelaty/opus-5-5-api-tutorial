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

from agent.agent import (ASSETS_DIR, MAX_SECONDS, MAX_TURNS, STOP_THRESHOLD_USD, TASK_BUDGET_TOKENS,
                         run_investigation)
from agent.cost import (MODEL, PRICE_CACHE_READ, PRICE_CACHE_WRITE_1H, PRICE_CACHE_WRITE_5M, PRICE_INPUT,
                        PRICE_OUTPUT, PRICE_WEB_SEARCH_PER_1000)
from agent.evidence import RED_HERRINGS

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
RUNTIME_ASSETS = Path(ASSETS_DIR)
RUNS_DIR = HERE / "runs"
DATACAMP_URL = "https://www.datacamp.com/blog/opus-5-5"

FEATURES = [
    (":material/psychology:", "Adaptive thinking and effort",
     "Production runs at medium. A high-effort replay plan is built from the same evidence snapshot for "
     "comparison only."),
    (":material/code:", "Programmatic tool calling",
     "Logs, traces, and metrics can only be queried from Claude's sandboxed code, so raw rows stay there."),
    (":material/rule:", "Strict control tools",
     "Deployment context and the evidence-ledger tools are direct-only strict tools. Python enforces both paths."),
    (":material/image_search:", "Screenshots as evidence",
     "The dashboard and architecture diagram suggest hypotheses that the metrics then have to confirm."),
    (":material/science:", "Counterfactual replay",
     "The application reruns the same traffic with the changes in Claude's replay plan."),
    (":material/data_object:", "Structured report",
     "A separate request with no tools turns the evidence ledger into JSON. Cited IDs are checked before "
     "it is accepted."),
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
    budget = "none (pilot)" if TASK_BUDGET_TOKENS is None else f"{TASK_BUDGET_TOKENS:,} tokens (advisory)"
    st.markdown(
        f"- Model: `{MODEL}`\n"
        f"- Effort: `medium` (high plan for comparison only)\n"
        f"- Investigation task budget: {budget}\n"
        f"- App limits: {MAX_TURNS} turns, {MAX_SECONDS // 60} min, "
        f"no new request after ${STOP_THRESHOLD_USD:.2f}"
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
    if (RUNTIME_ASSETS / "architecture_diagram.png").exists():
        st.image(str(RUNTIME_ASSETS / "architecture_diagram.png"), caption="Checkout path architecture",
                 width="stretch")

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
                     help="Starts a live run that calls the Claude API.")
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


def plan_rows(plan: dict | None) -> list[dict]:
    return [{"Scenario": s["name"], "Changes": scenario_label(s), "Purpose": s["purpose"]}
            for s in (plan or {}).get("scenarios", [])]


def render_run(record: dict) -> None:
    report = record["report"]
    if "error" in report:
        st.error(f"The run ended without an accepted report: {report['error']}")
        if record.get("report_problems"):
            st.markdown("\n".join(f"- {p}" for p in record["report_problems"]))
        return

    verdict, confidence = report["verdict"], report["confidence"]
    st.markdown(
        f"<span class='pill pill-{verdict}'>Verdict: {verdict}</span>"
        f"<span class='pill pill-{confidence}'>Confidence: {confidence}</span>",
        unsafe_allow_html=True,
    )
    c, inv = record["cost"], record["investigation"]
    st.markdown(stats_html([
        ("Wall-clock time", f"{record['t']:.0f} s"),
        ("Billed API calls", str(c["billed_calls"])),
        ("Direct tool calls", str(inv["direct_tool_calls"])),
        ("Calls from code", str(inv["ptc_tool_calls"])),
        ("Ledger entries", str(len(record["evidence_ledger"]["findings"])
                               + len(record["evidence_ledger"]["hypotheses"]))),
        ("Cost", f"${c['total_cost']:.4f}"),
    ]), unsafe_allow_html=True)

    tabs = st.tabs([":material/description: Report", ":material/science: Replay", ":material/tune: Effort",
                    ":material/payments: Cost", ":material/insights: Evidence", ":material/data_object: JSON"])
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
            st.markdown("**Alternative explanations examined**")
            for item in report["alternatives"]:
                st.markdown(f"- *{item['alternative']}*, {item['conclusion']}: {item['reason']}")
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
                "ID": rp["id"],
                "Scenario": scenario_label(rp["scenario"]),
                "503s": rp["total_503s"],
                "On cart and order reads": rp["read_request_503s"],
                "Pool timeouts": rp["pool_timeout_503s"],
                "Pool saturated (s)": rp["seconds_pool_saturated"],
                "Checkout p95 (s)": round(rp["p95_latency_ms_checkout_post"] / 1000, 1),
            } for rp in record["replays"]]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            st.caption("The baseline plus each scenario in the medium-effort replay plan, run by the application.")
    with tabs[2]:
        plans = record.get("replay_plans", {})
        left, right = st.columns(2)
        for col, effort, note in ((left, "medium", "Production plan, the one that was run."),
                                  (right, "high", "Evaluation only: same evidence snapshot, never run.")):
            with col:
                p = plans.get(effort)
                st.markdown(f"**{effort.capitalize()} effort**")
                st.caption(note)
                if p and p.get("plan"):
                    st.dataframe(pd.DataFrame(plan_rows(p["plan"])), hide_index=True, width="stretch")
                    st.caption(f"{p['output_tokens']:,} output tokens, ${p['cost']:.4f}")
                else:
                    st.write("Not produced in this run.")
        if record.get("plan_comparison"):
            st.json(record["plan_comparison"], expanded=False)
    with tabs[3]:
        k = st.columns(4)
        k[0].metric("Uncached input", f"{c['uncached_input_tokens']:,}", f"${c['cost_uncached_input']:.4f}",
                    delta_color="off")
        k[1].metric("Cache read", f"{c['cache_read_tokens']:,}", f"${c['cost_cache_read']:.4f}", delta_color="off")
        k[2].metric("Cache write", f"{c['cache_write_tokens']:,}", f"${c['cost_cache_write']:.4f}",
                    delta_color="off")
        k[3].metric("Output", f"{c['output_tokens']:,}", f"${c['cost_output']:.4f}", delta_color="off")
        st.dataframe(pd.DataFrame([{"Phase": p, "API calls": v["api_calls"], "Output tokens": v["output_tokens"],
                                    "Cost": round(v["total_cost"], 4)} for p, v in c["by_phase"].items()]),
                     hide_index=True, width="stretch")
        if record.get("calls"):
            calls = pd.DataFrame(record["calls"])
            calls = calls[(calls[["input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"]]
                           .sum(axis=1)) > 0]
            st.dataframe(calls[["phase", "turn", "effort", "stop_reason", "input_tokens", "cache_read_tokens",
                                "cache_write_tokens", "output_tokens", "cost"]],
                         hide_index=True, width="stretch")
            st.caption("Calls that only resumed paused code execution report zero usage and are hidden.")
    with tabs[4]:
        if (RUNTIME_ASSETS / "monitoring_dashboard.png").exists():
            st.image(str(RUNTIME_ASSETS / "monitoring_dashboard.png"), width="stretch")
        ledger = record["evidence_ledger"]
        st.markdown("**Hypotheses**")
        for h in ledger["hypotheses"]:
            st.markdown(f"- `{h['id']}` *{h['confidence']}*, from {', '.join(h['source_ids'])}: {h['hypothesis']}")
        st.markdown("**Findings**")
        for f in ledger["findings"]:
            tag = f" ({RED_HERRINGS[f['alternative']]})" if f["alternative"] in RED_HERRINGS else ""
            st.markdown(f"- `{f['id']}`{tag} from {', '.join(f['source_ids'])}: {f['claim']}")
        if ledger["documentation"]:
            st.markdown("**Documentation**")
            for d in ledger["documentation"]:
                st.markdown(f"- `{d['id']}` [{d['url']}]({d['url']}): {d['claim']}")
        st.markdown("**Evidence citations**")
        for item in report["evidence_citations"]:
            st.markdown(f"- {item}")
    with tabs[5]:
        st.json(report)


def latest_run() -> dict | None:
    paths = sorted(glob.glob(str(RUNS_DIR / "measured" / "run_*.json"))) or sorted(
        glob.glob(str(RUNS_DIR / "run_*.json")))
    if not paths:
        return None
    with open(paths[-1], encoding="utf-8") as f:
        return json.load(f)


if live:
    counters = {"turns": 0, "direct": 0, "ptc": 0, "phase": "investigation", "cost": 0.0, "t": 0.0}
    stats_slot = st.empty()

    def show_counters():
        stats_slot.markdown(stats_html([
            ("Elapsed", f"{counters['t']:.0f} s"),
            ("Phase", counters["phase"]),
            ("Investigation turns", str(counters["turns"])),
            ("Direct tool calls", str(counters["direct"])),
            ("Calls from code", str(counters["ptc"])),
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
            if kind == "phase":
                counters["phase"] = event["phase"]
                log.markdown(f"{stamp} :material/flag: **{event['phase']}**")
            elif kind == "usage":
                counters["turns"] = event["turn"]
                counters["cost"] = event["running_total"]
            elif kind == "progress":
                log.markdown(f"{stamp} :material/chat: {code(event['text'], 300)}")
            elif kind == "tool_call":
                counters["ptc" if event["via_ptc"] else "direct"] += 1
                via = ":orange-badge[code]" if event["via_ptc"] else ":blue-badge[direct]"
                log.markdown(f"{stamp} {via} `{event['name']}` `{code(event['input'])}`")
            elif kind == "tool_result" and event["is_error"]:
                log.markdown(f"{stamp} :red-badge[rejected] `{event['name']}`: {code(event['error'], 200)}")
            elif kind in ("finding", "hypothesis", "documentation"):
                e = event["entry"]
                log.markdown(f"{stamp} :material/lightbulb: {kind} `{event['id']}`: "
                             f"{code(e.get('claim') or e.get('hypothesis'), 260)}")
            elif kind == "plan":
                names = [s["name"] for s in (event["plan"] or {}).get("scenarios", [])]
                log.markdown(f"{stamp} :material/checklist: {event['effort']} replay plan: {code(names, 260)}")
            elif kind == "replay":
                r = event["result"]
                log.markdown(f"{stamp} :material/science: replay `{event['id']}` {event['name']} -> "
                             f"**{r['total_503s']}** 503s, {r['read_request_503s']} on read endpoints, "
                             f"pool saturated {r['seconds_pool_saturated']} s")
            elif kind == "report_check":
                state = "accepted" if event["accepted"] else f"rejected: {code(event['problems'], 200)}"
                log.markdown(f"{stamp} :material/fact_check: report {state}")
            elif kind == "nudge":
                log.markdown(f"{stamp} :material/info: {code(event['text'], 200)}")
            elif kind == "done":
                record = event
                counters["cost"] = event["cost"]["total_cost"]
                status.update(label=f"Investigation finished ({event['stop']})", state="complete",
                              expanded=False)
            show_counters()

    if record is not None:
        out_dir = RUNS_DIR / "live"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        with open(out_dir / f"run_{stamp}.json", "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        st.session_state.spend = st.session_state.get("spend", 0.0) + record["cost"]["total_cost"]
        st.session_state.record = record

if recorded:
    st.session_state.record = latest_run()
    if st.session_state.record is None:
        st.info("No recorded runs yet. Run `python run_experiment.py` or start a live run first.")

if st.session_state.get("record"):
    st.divider()
    render_run(st.session_state.record)

