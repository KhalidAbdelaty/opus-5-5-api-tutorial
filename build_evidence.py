"""Generate the synthetic HarborCart evidence packet from sim.simulate().

Ground truth (never sent to the model):
  - Deployment dep-8841 at 09:14:00 UTC changed the payment client's urllib3
    Retry policy: allowed_methods=None (POST is now retried), 503 added to
    status_forcelist, backoff_factor=0 (retries fire back to back).
  - checkout-api holds its DB connection for the whole request, including
    every gateway attempt. That was already true before the deploy.
  - At 09:19:30 UTC the third-party payment gateway has a 90-second burst of
    slow 503s. This is the TRIGGER.
  - Retried POST attempts multiply how long each connection is held, the
    15-connection pool saturates, and requests that never call the gateway
    (GET /cart, GET /orders/{id}) start failing with db_pool_checkout_timeout.
    That spread from checkout POSTs to the whole service is the incident.
  - Red herrings: an inventory low-stock warning, a deprecated frontend
    analytics snippet, and CPU that stays near normal (so "the database or
    the host is overloaded" does not fit the data).
"""
import json
import os
import random
from datetime import timedelta

import sim

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence")
os.makedirs(OUT, exist_ok=True)

METRIC_STEP_S = 5
RATE_WINDOW_S = 30
ALERT_THRESHOLD_PCT = 2.0


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ms(seconds):
    return int(round(seconds * 1000))


requests = sim.simulate()

# --------------------------------------------------------------------------
# Deployment metadata, diff, runbook
# --------------------------------------------------------------------------
deployment_metadata = {
    "deployment_id": "dep-8841",
    "service": "checkout-api",
    "environment": "production",
    "started_at": iso(sim.DEPLOY_AT),
    "completed_at": iso(sim.DEPLOY_AT + timedelta(seconds=47)),
    "commit_sha": "a3f9c2e",
    "previous_commit_sha": "6b1d0aa",
    "changed_files": ["checkout/payment_client.py"],
    "deployed_by": "ci-bot",
    "rollout_strategy": "rolling",
    "instances_updated": 6,
}
with open(f"{OUT}/deployment_metadata.json", "w") as f:
    json.dump(deployment_metadata, f, indent=2)

diff = '''diff --git a/checkout/payment_client.py b/checkout/payment_client.py
index 6b1d0aa..a3f9c2e 100644
--- a/checkout/payment_client.py
+++ b/checkout/payment_client.py
@@ -8,10 +8,10 @@ def build_payment_session() -> requests.Session:
     """Session used for all outbound calls to the payment gateway."""
     session = requests.Session()
     retry = Retry(
-        total=3,
-        status_forcelist={502, 504},
-        allowed_methods={"GET"},
-        backoff_factor=0.5,
+        total=3,
+        status_forcelist={502, 503, 504},
+        allowed_methods=None,
+        backoff_factor=0,
     )
     adapter = HTTPAdapter(max_retries=retry)
     session.mount("https://", adapter)
     return session
'''
with open(f"{OUT}/deploy_diff.patch", "w") as f:
    f.write(diff)

runbook = f"""# HarborCart Checkout Service Runbook

## Overview
`checkout-api` serves the cart and order pages (`GET /cart`,
`GET /orders/{{id}}`) and cart-to-order conversion (`POST /checkout`). For a
checkout it validates the cart, opens a database transaction against
`checkout_db`, calls the external payment gateway, and commits the order on
a successful charge. All endpoints share one connection pool per instance.
It runs as 6 instances; the logs, traces, and metrics in this packet come
from `checkout-api-3`, the instance that paged first. `checkout_5xx_rate`
covers every endpoint on the instance.

## Dependencies
- PostgreSQL (`checkout_db`), accessed through a SQLAlchemy `QueuePool`
  with `pool_size=5`, `max_overflow=10` ({sim.POOL_CAPACITY} connections total per
  instance), `pool_timeout={int(sim.POOL_TIMEOUT_S)}` seconds.
- Payment gateway (`payments.harborcart.internal`), a third-party service
  outside our infrastructure. Its documented SLA is 99.9% availability;
  short error bursts during its own deploys are not unusual and normally
  self-resolve in under two minutes.

## Known alert: checkout 5xx rate above {ALERT_THRESHOLD_PCT:g}%
1. Check `checkout_5xx_rate` and `payment_gateway_5xx_rate` on the
   checkout dashboard. If the gateway rate is also elevated, note when it
   started and when it returned to zero.
2. Check `db_pool_in_use` against the pool capacity ({sim.POOL_CAPACITY}). Pool
   exhaustion produces 503s from `checkout-api` itself (connection checkout
   timeout), which look identical to the client as a generic 503 but
   originate from the application, not the gateway.
3. Compare when checkout 503s stop with when gateway 503s stop. A checkout
   503 tail that outlasts the gateway incident points at something inside
   checkout-api, not the gateway itself.
4. Check whether a deploy to `checkout-api` (not the gateway) preceded the
   incident.

## Connection handling contract
`checkout-api` acquires its database connection at the start of
`handle_checkout()` and holds it open through validation, the payment
gateway call, and the final commit. This is intentional: HarborCart wants
a single atomic transaction per order rather than a two-phase commit. The
tradeoff is that the connection's hold time is proportional to however
long the payment call takes, including any retries the HTTP client
performs before giving up.

## Rollback
`kubectl rollout undo deployment/checkout-api` reverts to the previous
image. Rolling back does not require a database migration.
"""
with open(f"{OUT}/runbook.md", "w") as f:
    f.write(runbook)

# --------------------------------------------------------------------------
# Logs and traces
# --------------------------------------------------------------------------
app_logs, traces, gateway_logs = [], [], []


def log(ts, level, message, request_id):
    app_logs.append({"timestamp": iso(ts), "service": "checkout-api", "instance": "checkout-api-3",
                     "level": level, "message": message, "request_id": request_id})


for r in requests:
    rid = r.request_id
    arrival = sim.at(r.arrival)
    finish = sim.at(r.finish)
    duration_ms = ms(r.duration)
    route = f"{r.method} {r.path}"
    base = {"request_id": rid, "method": r.method, "path": r.path, "start": iso(arrival),
            "duration_ms": duration_ms}

    if r.outcome == "503_pool_timeout":
        log(finish, "ERROR",
            f"{route} failed status=503 reason=db_pool_checkout_timeout "
            f"waited_ms={ms(r.wait)} pool_in_use={sim.POOL_CAPACITY} pool_capacity={sim.POOL_CAPACITY}", rid)
        traces.append({**base, "spans": [{"name": "db.acquire_connection", "duration_ms": ms(r.wait),
                                           "status": "timeout"}],
                       "outcome": r.outcome})
        continue

    wait_note = f" pool_wait_ms={ms(r.wait)}" if r.wait > 0 else ""
    spans = [{"name": "db.acquire_connection", "duration_ms": max(ms(r.wait), 4)}]
    if not r.is_checkout:
        spans.append({"name": "db.query", "duration_ms": ms(r.hold_end - r.hold_start)})
        log(finish, "INFO", f"{route} completed status=200 duration_ms={duration_ms}{wait_note}", rid)
        traces.append({**base, "spans": spans, "outcome": r.outcome})
        continue

    for n, a in enumerate(r.attempts, start=1):
        spans.append({"name": "payment_gateway.charge", "method": "POST", "attempt": n,
                      "duration_ms": ms(a.duration), "status": a.status})
        if a.status == 503:
            gateway_logs.append({"timestamp": iso(sim.at(a.start + a.duration)),
                                 "service": "payment-gateway", "level": "ERROR",
                                 "message": "HTTP 503 Service Unavailable (upstream capacity) "
                                            "method=POST path=/v1/charges"})
    hold_ms = ms(r.hold_end - r.hold_start)
    attempts = len(r.attempts)

    if r.outcome == "200":
        spans.append({"name": "db.commit_and_release", "duration_ms": 6})
        if attempts > 1:
            log(finish, "WARN",
                f"{route} completed after retries status=200 attempts={attempts} "
                f"duration_ms={duration_ms} conn_hold_ms={hold_ms}{wait_note}", rid)
        else:
            log(finish, "INFO", f"{route} completed status=200 duration_ms={duration_ms}{wait_note}", rid)
    else:
        spans.append({"name": "db.rollback_and_release", "duration_ms": 5})
        log(finish, "ERROR",
            f"{route} failed status=503 reason=payment_gateway_unavailable attempts={attempts} "
            f"duration_ms={duration_ms} conn_hold_ms={hold_ms}{wait_note}", rid)
    traces.append({**base, "spans": spans, "outcome": r.outcome})

app_logs.sort(key=lambda r: r["timestamp"])
traces.sort(key=lambda r: r["start"])
gateway_logs.sort(key=lambda r: r["timestamp"])

for name, rows in (("app_logs.jsonl", app_logs), ("payment_gateway_logs.jsonl", gateway_logs),
                   ("traces.jsonl", traces)):
    with open(f"{OUT}/{name}", "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

# --------------------------------------------------------------------------
# Red herrings
# --------------------------------------------------------------------------
inventory_log = {
    "timestamp": iso(sim.BURST_START + timedelta(minutes=1)),
    "service": "inventory-service", "level": "WARN",
    "message": "SKU HC-2291 (canvas tote, navy) below reorder threshold (4 remaining)",
}
frontend_log = {
    "timestamp": iso(sim.BURST_START + timedelta(seconds=40)),
    "service": "web-frontend", "level": "WARN",
    "message": "Deprecated analytics snippet 'legacyTrack()' called on /checkout; "
               "scheduled for removal in Q4. No functional impact.",
}
with open(f"{OUT}/inventory_service_warning.log", "w") as f:
    f.write(json.dumps(inventory_log) + "\n")
with open(f"{OUT}/frontend_console_warning.log", "w") as f:
    f.write(json.dumps(frontend_log) + "\n")

# --------------------------------------------------------------------------
# Metrics (5-second samples, rates over a trailing 30-second window)
# --------------------------------------------------------------------------
attempt_events = [(a.start + a.duration, a.status) for r in requests for a in r.attempts]
noise = random.Random(7)
metrics = {name: [] for name in ("checkout_p95_latency_ms", "db_pool_in_use", "checkout_5xx_rate",
                                 "payment_gateway_5xx_rate", "cpu_percent")}
alert_at = None
t = 0.0
end = sim.offset(sim.TRAFFIC_END)
while t <= end:
    done = [r for r in requests if t - RATE_WINDOW_S < r.finish <= t]
    failed = sum(1 for r in done if r.outcome != "200")
    rate = round(100.0 * failed / len(done), 1) if done else 0.0
    durations = sorted(r.duration for r in done)
    p95 = ms(durations[int(0.95 * (len(durations) - 1))]) if durations else 0
    gw = [s for (ts, s) in attempt_events if t - RATE_WINDOW_S < ts <= t]
    gw_rate = round(100.0 * sum(1 for s in gw if s == 503) / len(gw), 1) if gw else 0.0
    in_use = sim.pool_in_use(requests, t)
    # Threads blocked on the gateway or the pool wait on I/O, so CPU barely moves.
    cpu = round(28 + min(in_use, 15) * 0.4 + noise.uniform(-2.5, 2.5), 1)

    ts = iso(sim.at(t))
    metrics["checkout_p95_latency_ms"].append({"t": ts, "v": p95})
    metrics["db_pool_in_use"].append({"t": ts, "v": in_use})
    metrics["checkout_5xx_rate"].append({"t": ts, "v": rate})
    metrics["payment_gateway_5xx_rate"].append({"t": ts, "v": gw_rate})
    metrics["cpu_percent"].append({"t": ts, "v": cpu})
    if alert_at is None and rate > ALERT_THRESHOLD_PCT:
        alert_at = ts
    t += METRIC_STEP_S

with open(f"{OUT}/metrics.json", "w") as f:
    json.dump(metrics, f)

alert = {
    "alert": f"checkout-api 5xx rate above {ALERT_THRESHOLD_PCT:g}%",
    "service": "checkout-api",
    "instance": "checkout-api-3",
    "fired_at": alert_at,
    "window_seconds": RATE_WINDOW_S,
}
with open(f"{OUT}/alert.json", "w") as f:
    json.dump(alert, f, indent=2)

summary = sim.summarize(requests)
print("Evidence packet written to", OUT)
print("Deploy:", iso(sim.DEPLOY_AT), "| gateway burst:", iso(sim.BURST_START),
      f"for {sim.BURST_SECONDS:g}s | alert fired:", alert_at)
print(json.dumps(summary, indent=2))
