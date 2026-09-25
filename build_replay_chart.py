"""Render the counterfactual replay chart: 503s by cause for the deployed
configuration and each one-change scenario, from the same sim.simulate()
the replay tool runs.

    python build_replay_chart.py
"""
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

import sim

HERE = os.path.dirname(os.path.abspath(__file__))

SCENARIOS = [
    ("As deployed", {}),
    ("Retry policy\nreverted", {"retry_policy": "previous"}),
    ("Connection released\nbefore gateway call", {"release_connection_before_gateway": True}),
    ("Pool raised\nto 30", {"pool_capacity": 30}),
    ("No gateway\nburst", {"gateway_burst": False}),
]

labels, gateway, pool_checkout, pool_read = [], [], [], []
for label, kwargs in SCENARIOS:
    reqs = sim.simulate(**kwargs)
    labels.append(label)
    gateway.append(sum(1 for r in reqs if r.outcome == "503_gateway"))
    pool_checkout.append(sum(1 for r in reqs if r.outcome == "503_pool_timeout" and r.is_checkout))
    pool_read.append(sum(1 for r in reqs if r.outcome == "503_pool_timeout" and not r.is_checkout))

fig, ax = plt.subplots(figsize=(11, 5.5))
x = range(len(labels))
ax.bar(x, gateway, color="#e67e22", label="Gateway 503 passed through (POST /checkout)")
ax.bar(x, pool_checkout, bottom=gateway, color="#c0392b", label="Pool timeout on POST /checkout")
bottom = [g + p for g, p in zip(gateway, pool_checkout)]
ax.bar(x, pool_read, bottom=bottom, color="#7b241c", label="Pool timeout on GET /cart and /orders")
for i, total in enumerate(g + p + q for g, p, q in zip(gateway, pool_checkout, pool_read)):
    ax.text(i, total + 2, str(total), ha="center", fontsize=11, fontweight="bold")
ax.set_xticks(list(x))
ax.set_xticklabels(labels, fontsize=10)
ax.set_ylabel("503 responses during the incident")
ax.set_title("Same traffic, one change at a time: where the 503s come from", fontsize=13, fontweight="bold")
ax.legend(loc="upper right", fontsize=9)
ax.grid(axis="y", alpha=0.3)
ax.set_ylim(0, max(bottom[i] + pool_read[i] for i in range(len(labels))) * 1.18)
fig.tight_layout()

out_dir = os.environ.get("HARBORCART_ASSETS_DIR") or os.path.join(HERE, "assets")
os.makedirs(out_dir, exist_ok=True)
out = os.path.join(out_dir, "counterfactual_replay.png")
fig.savefig(out, dpi=140)
print("wrote", out)
