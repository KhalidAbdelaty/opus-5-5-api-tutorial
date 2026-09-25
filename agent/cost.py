"""Cost tracking for Claude Opus 5.5 API calls, computed from response usage.

Rates are the official per-million-token prices as of September 25, 2026
(https://platform.claude.com/docs/en/about-claude/pricing).

`usage.input_tokens` already excludes cached tokens: it counts only what
comes after the last cache breakpoint. Total input is
input_tokens + cache_read_input_tokens + cache_creation_input_tokens.

Code execution adds no container charge here: it only runs in investigation
requests, and every one of them includes web_search_20260318, which makes
code execution free beyond token costs. The replay-plan and report requests
have no tools. Results of programmatic tool calls do not count toward token
usage; only the code's final output and Claude's response do.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MODEL = "claude-opus-5-5"

PRICE_INPUT = 4.00
PRICE_OUTPUT = 20.00
PRICE_CACHE_READ = 0.20
PRICE_CACHE_WRITE_5M = 5.00
PRICE_CACHE_WRITE_1H = 8.00
PRICE_WEB_SEARCH_PER_1000 = 10.00


@dataclass
class CallCost:
    label: str
    phase: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    web_searches: int = 0

    @property
    def cache_write_tokens(self) -> int:
        return self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def billed(self) -> bool:
        return bool(self.total_input_tokens or self.output_tokens or self.web_searches)

    @property
    def cost(self) -> float:
        token_cost = (
            self.input_tokens * PRICE_INPUT
            + self.cache_read_tokens * PRICE_CACHE_READ
            + self.cache_write_5m_tokens * PRICE_CACHE_WRITE_5M
            + self.cache_write_1h_tokens * PRICE_CACHE_WRITE_1H
            + self.output_tokens * PRICE_OUTPUT
        ) / 1_000_000
        return token_cost + self.web_searches * PRICE_WEB_SEARCH_PER_1000 / 1000


def _get(obj, name: str) -> int:
    return (getattr(obj, name, 0) or 0) if obj is not None else 0


@dataclass
class CostLedger:
    calls: list[CallCost] = field(default_factory=list)
    verbose: bool = False

    def add(self, label: str, usage, phase: str = "investigation") -> CallCost:
        """`usage` is the `usage` object of a Messages API response."""
        creation = getattr(usage, "cache_creation", None)
        if creation is not None:
            write_5m = _get(creation, "ephemeral_5m_input_tokens")
            write_1h = _get(creation, "ephemeral_1h_input_tokens")
        else:
            write_5m, write_1h = _get(usage, "cache_creation_input_tokens"), 0
        call = CallCost(
            label=label,
            phase=phase,
            input_tokens=_get(usage, "input_tokens"),
            output_tokens=_get(usage, "output_tokens"),
            cache_read_tokens=_get(usage, "cache_read_input_tokens"),
            cache_write_5m_tokens=write_5m,
            cache_write_1h_tokens=write_1h,
            web_searches=_get(getattr(usage, "server_tool_use", None), "web_search_requests"),
        )
        self.calls.append(call)
        if self.verbose:
            print(f"  [cost] {label}: {call.input_tokens} uncached in, {call.cache_read_tokens} cache read, "
                  f"{call.cache_write_tokens} cache write, {call.output_tokens} out, "
                  f"{call.web_searches} searches -> ${call.cost:.4f} (total ${self.total:.4f})")
        return call

    @property
    def total(self) -> float:
        return sum(c.cost for c in self.calls)

    def breakdown(self) -> dict:
        data = _summarize(self.calls)
        phases = list(dict.fromkeys(c.phase for c in self.calls))
        data["by_phase"] = {p: _summarize([c for c in self.calls if c.phase == p]) for p in phases}
        return data


def _summarize(calls: list[CallCost]) -> dict:
    def total(attr):
        return sum(getattr(c, attr) for c in calls)

    return {
        "uncached_input_tokens": total("input_tokens"),
        "cache_read_tokens": total("cache_read_tokens"),
        "cache_write_tokens": total("cache_write_tokens"),
        "output_tokens": total("output_tokens"),
        "web_searches": total("web_searches"),
        "total_input_tokens": total("total_input_tokens"),
        "cost_uncached_input": total("input_tokens") * PRICE_INPUT / 1e6,
        "cost_cache_read": total("cache_read_tokens") * PRICE_CACHE_READ / 1e6,
        "cost_cache_write": (total("cache_write_5m_tokens") * PRICE_CACHE_WRITE_5M
                             + total("cache_write_1h_tokens") * PRICE_CACHE_WRITE_1H) / 1e6,
        "cost_output": total("output_tokens") * PRICE_OUTPUT / 1e6,
        "cost_web_search": total("web_searches") * PRICE_WEB_SEARCH_PER_1000 / 1000,
        "total_cost": total("cost"),
        "api_calls": len(calls),
        "billed_calls": sum(1 for c in calls if c.billed),
    }
