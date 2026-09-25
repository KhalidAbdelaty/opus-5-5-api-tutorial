# HarborCart Incident Investigator (Claude Opus 5.5)

An incident-investigation agent built on the Claude Opus 5.5 API. It investigates a fictional checkout outage from logs, traces, metrics, a deployment diff, and two screenshots, builds an evidence ledger, tests the leading root cause with counterfactual replays, and returns a structured incident report.

## How a run works

1. **Investigation.** Claude uses vision, programmatic tool calling over logs, traces, and metrics, direct strict tools that write to the evidence ledger, and web search limited to the retry library's docs. This request has no output schema, because web search results carry citations and citations cannot be combined with structured outputs. It ends when Claude calls `finish_investigation` and the ledger checks pass.
2. **Replay plan.** The evidence snapshot, frozen before any replay, goes to a request with a replay-plan JSON schema and no tools, at effort `medium`.
3. **Replay.** The application runs that plan with the same simulation that generated the evidence. Claude cannot call the replay.
4. **Report.** The ledger and replay outputs go to a separate request with the incident-report JSON schema and no tools. The application checks every cited evidence ID before it accepts the report.

For evaluation only, the same snapshot is also sent with a per-message effort of `high`. That plan is saved and compared with the medium plan, but it is never run and neither request sees the other's answer.

Two rules are enforced in Python rather than left to the model:

- Log, trace, and metric queries only run when called from code execution. Deployment context and the ledger tools only run when called directly. `allowed_callers` guides the model but is not a security boundary.
- The investigation cannot finish, and the report is not accepted, until at least two of the three seeded red herrings have been examined against evidence that covers them: the inventory-service warning, the web-frontend warning, and host CPU saturation.

## Setup

You need Python 3.10 or newer and an Anthropic API key with access to `claude-opus-5-5`.

```bash
git clone https://github.com/KhalidAbdelaty/opus-5-5-api-tutorial.git
cd opus-5-5-api-tutorial
python -m venv .venv
.venv\Scripts\Activate.ps1        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
Copy-Item .env.example .env       # macOS/Linux: cp .env.example .env
```

Then put your key in `.env`.

The agent receives a dashboard screenshot and an architecture diagram as vision input. Generate them from the evidence before a live run. `HARBORCART_ASSETS_DIR` sets where they are written and read:

```bash
$env:HARBORCART_ASSETS_DIR = ".tmp/runtime-assets"   # macOS/Linux: export HARBORCART_ASSETS_DIR=.tmp/runtime-assets
python build_dashboard.py
python build_architecture.py
```

## Run it

```bash
python -m pytest                       # offline tests, no API calls
python first_call.py                   # smallest request: prints the content block types
python check_tool_choice.py            # shows the 400 error for forced tool_choice
python run_investigation.py --pilot    # investigation only, no task budget
python run_investigation.py            # one full run with the measured task budget
python run_experiment.py pilots        # 3 pilots, then derive the task budget
python run_experiment.py measured      # 3 full runs with that budget
python score_run.py                    # grades runs/measured/ against the ground-truth rubric
python -m streamlit run app_streamlit.py
```

The task budget applies to the investigation phase. Pilots run without it and measure what it counts: output tokens plus the tool-result tokens Claude saw. The budget is the largest pilot measurement plus 25%, rounded up to 5,000 tokens and never below the API minimum of 20,000. See `runs/pilot/budget.json`.

Cost control is a stop threshold, not a hard cap: no new request is launched once the threshold is reached, but a request already in flight can still take the total past it. `run_experiment.py` applies one $2.50 threshold across all pilots and measured runs, including spend listed in `runs/spend_adjustments.json`.

## Project layout

- `agent/agent.py`: request builders for each phase, the investigation loop, and the run orchestration
- `agent/evidence.py`: the evidence ledger, red-herring definitions, and report validation
- `agent/tools.py`: read-only evidence tools, the caller policy, and the counterfactual replay
- `agent/schema.py`: the replay-plan and incident-report JSON schemas
- `agent/cost.py`: cost ledger computed from API usage, per phase
- `sim.py`: the traffic simulation shared by the evidence generator and the replay
- `build_evidence.py`, `build_dashboard.py`, `build_architecture.py`, `build_replay_chart.py`: regenerate the evidence packet and images
- `evidence/`: the generated logs, traces, metrics, diff, runbook, and alert
- `runs/pilot/`, `runs/measured/`: recorded pilots, the derived budget, and measured runs
- `tests/`: offline tests for phase separation, caller enforcement, evidence IDs, replay determinism, and cost accounting

The evidence is deterministic. Running `build_evidence.py` reproduces the files in `evidence/`.
