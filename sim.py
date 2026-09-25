"""Checkout traffic simulation shared by the evidence generator and the
counterfactual replay tool.

Both callers run the same function, so a replay differs from the recorded
incident only in the parameter it changes. Arrival times come from one
seeded generator and every request draws its own random numbers from a
per-request seed, so changing the retry policy or the pool size does not
reshuffle the traffic (common random numbers).

Model of one checkout-api instance:
  - Two kinds of requests share one DB connection pool: POST /checkout
    (charges the payment gateway) and reads such as GET /cart and
    GET /orders/{id} (DB only, no gateway call).
  - A request waits for a DB connection. If none frees up within
    POOL_TIMEOUT_S, it fails with 503 (db_pool_checkout_timeout).
  - POST /checkout holds its connection through the gateway call. During
    the gateway's own 90-second burst, each attempt fails with
    BURST_FAILURE_PROBABILITY and the 503 arrives after several seconds.
  - The deployed retry policy allows up to 4 POST attempts back to back.
    The previous policy never retried POST, so one attempt only.
"""
from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

DEPLOY_AT = datetime(2026, 9, 24, 9, 14, 0, tzinfo=timezone.utc)
TRAFFIC_START = DEPLOY_AT - timedelta(minutes=3)
BURST_START = DEPLOY_AT + timedelta(minutes=5, seconds=30)
BURST_SECONDS = 90.0
TRAFFIC_END = DEPLOY_AT + timedelta(minutes=23)

POOL_CAPACITY = 15
POOL_TIMEOUT_S = 10.0
ARRIVAL_INTERVAL_S = (0.22, 0.34)
CHECKOUT_SHARE = 0.4
READ_PATHS = ("/cart", "/orders/{id}")
BURST_FAILURE_PROBABILITY = 0.8
FAILED_ATTEMPT_S = (6.0, 12.0)
OK_ATTEMPT_S = (0.12, 0.20)
READ_DB_S = (0.02, 0.05)
LOCAL_WORK_S = 0.03

POST_ATTEMPTS = {"deployed": 4, "previous": 1}
MAX_ATTEMPTS = max(POST_ATTEMPTS.values())


@dataclass
class Attempt:
    start: float
    duration: float
    status: int


@dataclass
class Request:
    index: int
    method: str
    path: str
    arrival: float
    wait: float
    outcome: str
    finish: float
    attempts: list[Attempt] = field(default_factory=list)
    hold_start: float | None = None
    hold_end: float | None = None

    @property
    def request_id(self) -> str:
        return f"req-{self.index:05d}"

    @property
    def duration(self) -> float:
        return self.finish - self.arrival

    @property
    def is_checkout(self) -> bool:
        return self.path == "/checkout"


def offset(dt: datetime) -> float:
    return (dt - TRAFFIC_START).total_seconds()


def at(seconds: float) -> datetime:
    return TRAFFIC_START + timedelta(seconds=seconds)


def simulate(retry_policy: str = "deployed", gateway_burst: bool = True,
             pool_capacity: int = POOL_CAPACITY,
             release_connection_before_gateway: bool = False,
             seed: int = 11) -> list[Request]:
    if retry_policy not in POST_ATTEMPTS:
        raise ValueError(f"retry_policy must be one of {sorted(POST_ATTEMPTS)}")
    if not 1 <= pool_capacity <= 500:
        raise ValueError("pool_capacity must be between 1 and 500")

    arrivals = random.Random(seed)
    horizon = offset(TRAFFIC_END)
    burst_start = offset(BURST_START)
    burst_end = burst_start + BURST_SECONDS
    max_attempts = POST_ATTEMPTS[retry_policy]

    held: list[float] = []
    requests: list[Request] = []
    t = 0.0
    index = 0
    while t < horizon:
        index += 1
        rng = random.Random(seed * 1_000_003 + index)
        is_checkout = rng.random() < CHECKOUT_SHARE
        read_path = READ_PATHS[rng.randrange(len(READ_PATHS))]
        read_s = rng.uniform(*READ_DB_S)
        draws = [(rng.random(), rng.uniform(*FAILED_ATTEMPT_S), rng.uniform(*OK_ATTEMPT_S))
                 for _ in range(MAX_ATTEMPTS)]
        method, path = ("POST", "/checkout") if is_checkout else ("GET", read_path)

        while held and held[0] <= t:
            heapq.heappop(held)

        wait = 0.0
        if len(held) >= pool_capacity:
            earliest = held[0]
            if earliest - t > POOL_TIMEOUT_S:
                requests.append(Request(index, method, path, t, POOL_TIMEOUT_S,
                                        "503_pool_timeout", t + POOL_TIMEOUT_S))
                t += arrivals.uniform(*ARRIVAL_INTERVAL_S)
                continue
            heapq.heappop(held)
            wait = earliest - t

        start = t + wait
        attempts: list[Attempt] = []
        if not is_checkout:
            finish = start + read_s
            hold_end = finish
            outcome = "200"
        else:
            clock = start + LOCAL_WORK_S / 2
            for p, failed_s, ok_s in draws[:max_attempts]:
                if gateway_burst and burst_start <= clock < burst_end and p < BURST_FAILURE_PROBABILITY:
                    attempts.append(Attempt(clock, failed_s, 503))
                    clock += failed_s
                else:
                    attempts.append(Attempt(clock, ok_s, 200))
                    clock += ok_s
                    break
            finish = clock + LOCAL_WORK_S / 2
            hold_end = start + LOCAL_WORK_S if release_connection_before_gateway else finish
            outcome = "200" if attempts[-1].status == 200 else "503_gateway"
        heapq.heappush(held, hold_end)
        requests.append(Request(index, method, path, t, wait, outcome, finish, attempts, start, hold_end))
        t += arrivals.uniform(*ARRIVAL_INTERVAL_S)

    return requests


def pool_in_use(requests: list[Request], t: float) -> int:
    return sum(1 for r in requests
               if r.hold_start is not None and r.hold_start <= t < r.hold_end)


def summarize(requests: list[Request], pool_capacity: int = POOL_CAPACITY) -> dict:
    burst_end = offset(BURST_START) + BURST_SECONDS
    failures = [r for r in requests if r.outcome != "200"]
    last_failure = max((r.finish for r in failures), default=None)
    window = [r for r in requests if offset(BURST_START) <= r.arrival < burst_end + 60]

    def p95(rows):
        values = sorted(r.duration for r in rows)
        return round(values[int(0.95 * (len(values) - 1))] * 1000) if values else 0

    saturated_s = sum(1 for s in range(int(offset(TRAFFIC_END)))
                      if pool_in_use(requests, float(s)) >= pool_capacity)
    return {
        "requests_modeled": len(requests),
        "total_503s": len(failures),
        "checkout_post_503s": sum(1 for r in failures if r.is_checkout),
        "read_request_503s": sum(1 for r in failures if not r.is_checkout),
        "pool_timeout_503s": sum(1 for r in failures if r.outcome == "503_pool_timeout"),
        "gateway_503s": sum(1 for r in failures if r.outcome == "503_gateway"),
        "retried_checkouts": sum(1 for r in requests if len(r.attempts) > 1),
        "seconds_pool_saturated": saturated_s,
        "p95_latency_ms_checkout_post": p95([r for r in window if r.is_checkout]),
        "p95_latency_ms_read_requests": p95([r for r in window if not r.is_checkout]),
        "errors_end_after_gateway_recovers_s": (
            round(last_failure - burst_end, 1) if last_failure is not None else None),
        "pool_exhaustion_occurred": any(r.outcome == "503_pool_timeout" for r in failures),
    }
