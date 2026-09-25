"""Render the architecture-diagram PNG the agent receives as vision evidence.

This shows the system topology (frontend, checkout-api, DB pool, payment
gateway, inventory service) so the model can see how the pieces connect. It
deliberately does not label which component is at fault; that is what the
investigation is for.
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

HERE = os.path.dirname(__file__)

fig, ax = plt.subplots(figsize=(11, 6.5))
ax.set_xlim(0, 11)
ax.set_ylim(0, 7)
ax.axis("off")
ax.set_title("HarborCart production architecture - checkout path", fontsize=13, fontweight="bold")

boxes = {
    "frontend": (0.5, 3.0, 2.0, 1.0, "web-frontend"),
    "checkout": (3.2, 3.0, 2.4, 1.0, "checkout-api\n(6 instances)"),
    "pool": (3.2, 1.0, 2.4, 1.0, "checkout_db\nconnection pool"),
    "gateway": (6.4, 3.0, 2.4, 1.0, "payment gateway\n(third-party)"),
    "inventory": (6.4, 5.0, 2.4, 1.0, "inventory-service"),
    "db": (3.2, -0.6+1.0, 0, 0, ""),
}

patches = {}
for key, (x, y, w, h, label) in boxes.items():
    if w == 0:
        continue
    box = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.08",
                          linewidth=1.4, edgecolor="#2c3e50", facecolor="#ecf0f1")
    ax.add_patch(box)
    ax.text(x + w / 2, y + h / 2, label, ha="center", va="center", fontsize=10)
    patches[key] = (x, y, w, h)

def arrow(a, b, label=""):
    ax1, ay1, aw, ah = patches[a]
    bx1, by1, bw, bh = patches[b]
    p1 = (ax1 + aw, ay1 + ah / 2)
    p2 = (bx1, by1 + bh / 2)
    if ay1 == by1 and ax1 == bx1:
        return
    arr = FancyArrowPatch(p1, p2, arrowstyle="-|>", mutation_scale=14,
                           linewidth=1.2, color="#34495e")
    ax.add_patch(arr)
    if label:
        mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2 + 0.15
        ax.text(mx, my, label, ha="center", fontsize=8, color="#34495e")

arrow("frontend", "checkout", "POST /checkout\nGET /cart, /orders")
arrow("checkout", "gateway", "charge request")
arrow("checkout", "inventory", "stock check (async)")

cx, cy, cw, ch = patches["checkout"]
px, py, pw, ph = patches["pool"]
arr = FancyArrowPatch((cx + cw / 2, cy), (px + pw / 2, py + ph), arrowstyle="-|>",
                       mutation_scale=14, linewidth=1.2, color="#34495e")
ax.add_patch(arr)
ax.text(cx + cw / 2 + 0.15, (cy + py + ph) / 2, "database\ntransaction",
        fontsize=8, color="#34495e")

out_dir = os.environ.get("HARBORCART_ASSETS_DIR") or os.path.join(HERE, "assets")
os.makedirs(out_dir, exist_ok=True)
out_path = os.path.join(out_dir, "architecture_diagram.png")
fig.tight_layout()
fig.savefig(out_path, dpi=140)
print("wrote", out_path)
