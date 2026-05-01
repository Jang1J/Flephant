"""S1-9 ops Monitor. SLA 추적 + kill_switch 자동 트리거 + rejection rate.

책임:
  - Hot Runner 각 단계(latency) 측정 → p50/p95/p99
  - Execution rejection rate
  - daily_pnl 누적 → KillSwitch 자동 트리거
  - SLA 위반 감지 → audit log + (선택) alert

모든 임계값은 risk_config.yaml (quant_agent.latency_p95_target_ms 등) 경유.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

import numpy as np

from src.execution.kill_switch import KillSwitch
from src.ops.audit_logger import AuditLogger
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("monitor")


class OpsMonitor:
    """운영 모니터. Hot Runner / Execution 사이클 메트릭 집계."""

    def __init__(
        self,
        kill_switch: KillSwitch | None = None,
        audit_logger: AuditLogger | None = None,
        window_size: int | None = None,
    ) -> None:
        self._kill = kill_switch
        self._audit = audit_logger

        # yaml 필수 (fallback 없음, 불변 원칙 5 준수)
        qa_cfg = config_load("risk_config.yaml", "quant_agent")
        self._window_size: int = int(
            window_size if window_size is not None else qa_cfg["latency_window"]
        )
        self._hot_path_sla_ms: float = float(qa_cfg["latency_p95_target_ms"])

        self._latencies: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self._window_size)
        )
        self._reject_count: dict[str, int] = defaultdict(int)
        self._total_count: int = 0
        self._daily_pnl: float = 0.0
        self._last_reset_ts: str = datetime.now(tz=timezone.utc).isoformat()

        logger.info(
            "[monitor] 초기화: window=%d, hot_sla_ms=%.1f",
            self._window_size, self._hot_path_sla_ms,
        )

    # ================================================================== #
    # Latency
    # ================================================================== #

    def record_latency(self, stage: str, ms: float) -> None:
        self._latencies[stage].append(float(ms))

    def latency_percentiles(self, stage: str) -> dict[str, float]:
        records = list(self._latencies.get(stage, []))
        if not records:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "n": 0}
        arr = np.asarray(records, dtype=float)
        return {
            "p50": float(np.percentile(arr, 50)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "max": float(arr.max()),
            "n": int(arr.size),
        }

    def all_latency_stats(self) -> dict[str, dict[str, float]]:
        return {stage: self.latency_percentiles(stage) for stage in self._latencies}

    # ================================================================== #
    # Rejection
    # ================================================================== #

    def record_execution_result(self, status: str, reason: str | None = None) -> None:
        self._total_count += 1
        if status == "rejected":
            key = reason or "unknown"
            self._reject_count[key] += 1

    def rejection_rate(self) -> dict[str, Any]:
        if self._total_count == 0:
            return {"total": 0, "rejected": 0, "rate": 0.0, "by_reason": {}}
        total_rejected = sum(self._reject_count.values())
        return {
            "total": self._total_count,
            "rejected": total_rejected,
            "rate": float(total_rejected / self._total_count),
            "by_reason": dict(self._reject_count),
        }

    # ================================================================== #
    # Daily PnL + KillSwitch
    # ================================================================== #

    def update_daily_pnl(self, pnl_delta: float) -> dict[str, Any]:
        self._daily_pnl += float(pnl_delta)
        triggered = False
        if self._kill and not self._kill.is_active():
            triggered = self._kill.check_daily_pnl(self._daily_pnl)
            if triggered and self._audit:
                self._audit.log("kill_switch_auto_trigger", {
                    "daily_pnl": self._daily_pnl,
                    "threshold": self._kill.threshold,
                })
        return {
            "daily_pnl": self._daily_pnl,
            "kill_switch_active": self._kill.is_active() if self._kill else False,
            "triggered_now": triggered,
        }

    def reset_daily(self) -> None:
        self._daily_pnl = 0.0
        self._reject_count.clear()
        self._total_count = 0
        self._last_reset_ts = datetime.now(tz=timezone.utc).isoformat()
        logger.info("[monitor] daily 메트릭 리셋")

    # ================================================================== #
    # SLA violations
    # ================================================================== #

    def check_sla_violations(self) -> list[dict[str, Any]]:
        violations: list[dict[str, Any]] = []
        hot_stages = ("quant", "ppo", "pm", "fda", "hot_loop")
        for stage in hot_stages:
            stats = self.latency_percentiles(stage)
            if stats["n"] < 5:
                continue
            if stats["p95"] > self._hot_path_sla_ms:
                violations.append({
                    "stage": stage,
                    "p95_ms": stats["p95"],
                    "sla_ms": self._hot_path_sla_ms,
                    "n": stats["n"],
                })
        if violations and self._audit:
            self._audit.log("sla_violation", {"violations": violations})
        return violations

    def summary(self) -> dict[str, Any]:
        return {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "last_reset": self._last_reset_ts,
            "latency": self.all_latency_stats(),
            "rejection": self.rejection_rate(),
            "daily_pnl": self._daily_pnl,
            "kill_switch_active": (
                self._kill.is_active() if self._kill else False
            ),
            "sla_violations": self.check_sla_violations(),
        }
