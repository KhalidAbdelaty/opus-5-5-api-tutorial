# HarborCart Incident Investigator (Claude Opus 5.5)

An incident-investigation agent built on the Claude Opus 5.5 API. It investigates a fictional checkout outage from logs, traces, metrics, a deployment diff, and two screenshots, tests its root cause with a counterfactual replay, and returns a structured incident report.

It uses adaptive thinking with per-message effort, a task budget, strict tools, programmatic tool calling, web search, prompt caching, and structured outputs.

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

## Run it

```bash
python first_call.py              # smallest request: prints the content block types
python check_tool_choice.py       # shows the 400 error for forced tool_choice
python run_investigation.py       # one full investigation, saved to runs/
python run_investigation.py --no-escalation   # same, but effort stays at medium
python score_run.py               # grades every saved run against the rubric
python -m streamlit run app_streamlit.py      # live UI
```

A full investigation takes about two minutes and cost $0.15 to $0.32 in the recorded runs. In the Streamlit app, **Show the latest recorded run** opens a saved run from `runs/` without calling the API.

## Project layout

- `agent/agent.py`: the investigation loop and request configuration
- `agent/tools.py`: read-only evidence tools and the counterfactual replay tool
- `agent/schema.py`: the incident report JSON schema
- `agent/cost.py`: cost ledger computed from API usage
- `sim.py`: the traffic simulation shared by the evidence generator and the replay tool
- `build_evidence.py`, `build_dashboard.py`, `build_architecture.py`, `build_replay_chart.py`: regenerate the evidence packet and images
- `evidence/`: the generated logs, traces, metrics, diff, runbook, and alert
- `runs/`: recorded investigation runs

The evidence is deterministic. Running the four `build_*.py` scripts reproduces the files in `evidence/` and `assets/`.
