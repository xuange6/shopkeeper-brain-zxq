"""Deterministic post-release canary/shadow guard.

No single stochastic model result can trigger rollback.  A rollback requires a
minimum sample count, consecutive unhealthy windows, and at least two distinct
signals (quality/latency/error/cost/dependency).  Operators can place a hold and
all automated actions observe a cooldown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class GuardConfig:
    min_samples: int = 50
    min_consecutive_windows: int = 2
    max_error_rate: float = 0.05
    min_success_rate: float = 0.95
    min_citation_validity: float = 0.98
    max_refusal_rate: float = 0.35
    max_p95_ms: float = 8000.0
    max_cost_per_query: float = 0.05
    cooldown_seconds: float = 900.0


@dataclass(frozen=True)
class HealthWindow:
    samples: int
    success_rate: float
    error_rate: float
    citation_validity: float
    refusal_rate: float
    p95_ms: float
    cost_per_query: float
    dependency_failures: int = 0


@dataclass(frozen=True)
class GuardDecision:
    action: str
    signals: tuple[str, ...]
    reason: str


class ReleaseGuard:
    def __init__(self, config: GuardConfig = GuardConfig()):
        self.config = config

    def evaluate(
        self,
        windows: Sequence[HealthWindow],
        *,
        now: float,
        last_action_at: float | None = None,
        manual_hold: bool = False,
    ) -> GuardDecision:
        if manual_hold:
            return GuardDecision("manual_hold", (), "operator hold is active")
        if last_action_at is not None and now - last_action_at < self.config.cooldown_seconds:
            return GuardDecision("cooldown", (), "automatic action cooldown is active")
        eligible = [window for window in windows if window.samples >= self.config.min_samples]
        if len(eligible) < self.config.min_consecutive_windows:
            return GuardDecision("observe", (), "insufficient canary samples")
        recent = eligible[-self.config.min_consecutive_windows :]
        common = set(self._signals(recent[0]))
        for window in recent[1:]:
            common.intersection_update(self._signals(window))
        signals = tuple(sorted(common))
        if len(signals) >= 2:
            return GuardDecision("rollback", signals, "multiple deterministic signals breached across consecutive windows")
        if signals:
            return GuardDecision("pause", signals, "single signal breached; pause promotion and require review")
        return GuardDecision("healthy", (), "all release health thresholds pass")

    def _signals(self, window: HealthWindow) -> set[str]:
        signals: set[str] = set()
        if window.success_rate < self.config.min_success_rate or window.error_rate > self.config.max_error_rate:
            signals.add("reliability")
        if window.citation_validity < self.config.min_citation_validity or window.refusal_rate > self.config.max_refusal_rate:
            signals.add("quality")
        if window.p95_ms > self.config.max_p95_ms:
            signals.add("latency")
        if window.cost_per_query > self.config.max_cost_per_query:
            signals.add("cost")
        if window.dependency_failures > 0:
            signals.add("dependency")
        return signals
