"""Render the monitoring-dashboard PNG the agent receives as vision evidence.

Built from the same evidence/metrics.json the query tools read, so what the
model sees in the image matches what it can query as numbers.
"""
import json
import os
from datetime import datetime, timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

import sim

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "evidence", "metrics.json")) as f:
    metrics = json.load(f)

VIEW_START = (sim.DEPLOY_AT - timedelta(minutes=1)).replace(tzinfo=None)
VIEW_END = (sim.BURST_START + timedelta(minutes=5)).replace(tzinfo=None)


def series(name):
    pts = metrics[name]
    ts = [datetime.strptime(p["t"], "%Y-%m-%dT%H:%M:%S.%fZ") for p in pts]
    return ts, [p["v"] for p in pts]


fig, axes = plt.subplots(4, 1, figsize=(11, 10), sharex=True)
fig.suptitle("HarborCart checkout-api-3 - production dashboard - 2026-09-24",
             fontsize=13, fontweight="bold")

ts, vs = series("checkout_p95_latency_ms")
axes[0].plot(ts, vs, color="#c0392b", linewidth=1.2)
axes[0].set_ylabel("checkout p95 (ms)")

ts, vs = series("checkout_5xx_rate")
axes[1].plot(ts, vs, color="#c0392b", linewidth=1.2, label="checkout_5xx_rate")
ts2, vs2 = series("payment_gateway_5xx_rate")
axes[1].plot(ts2, vs2, color="#e67e22", linewidth=1.2, label="payment_gateway_5xx_rate")
axes[1].set_ylabel("5xx rate (%, 30s)")
axes[1].legend(loc="upper right", fontsize=8)

ts, vs = series("db_pool_in_use")
axes[2].plot(ts, vs, color="#2980b9", linewidth=1.2)
axes[2].axhline(sim.POOL_CAPACITY, color="#7f8c8d", linestyle="--", linewidth=1,
                label=f"pool capacity ({sim.POOL_CAPACITY})")
axes[2].set_ylabel("db_pool_in_use")
axes[2].legend(loc="upper right", fontsize=8)

ts, vs = series("cpu_percent")
axes[3].plot(ts, vs, color="#27ae60", linewidth=1.2)
axes[3].set_ylabel("cpu %")
axes[3].set_ylim(0, 100)

for ax in axes:
    ax.grid(alpha=0.3)
    ax.set_xlim(VIEW_START, VIEW_END)
axes[3].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
axes[3].xaxis.set_major_locator(mdates.MinuteLocator())
axes[3].set_xlabel("UTC time")
fig.autofmt_xdate()
fig.tight_layout(rect=[0, 0, 1, 0.96])

out_path = os.path.join(HERE, "assets", "monitoring_dashboard.png")
os.makedirs(os.path.dirname(out_path), exist_ok=True)
fig.savefig(out_path, dpi=140)
print("wrote", out_path)
