"""S1-7 Safety Guards S-2~S-6. Hot Runner 1분 루프 + Execution 사이클 내 호출.

Guards (architecture.md §6.0.1):
  S-1 (별도 파일): KillSwitch: daily_loss 임계 자동 차단
  S-2: OAuth 갱신 확인 (AuthManager _need_refresh 래퍼)
  S-3: Sanity Check: 가격 이상치 (±sanity_max_change_pct 초과)
  S-4: Reconciliation: system_positions vs kis_actual_positions
  S-5: Audit log 기록 (AuditLogger 사용)
  S-6: Emergency halt: KillSwitch.trigger 래퍼 + 전량 청산 명령 (Sprint 4 실구현)

모든 임계값은 risk_config.yaml safety_guards 섹션 경유 (하드코딩 금지).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.execution.kill_switch import KillSwitch
from src.ops.audit_logger import AuditLogger
from src.utils.auth import AuthManager
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.ticker_utils import pad_ticker

logger = get_logger("safety_guards")


def _load_cfg() -> dict[str, Any]:
    """risk_config.yaml safety_guards 섹션 로드. 섹션 필수 (불변 원칙 5)."""
    return config_load("risk_config.yaml", "safety_guards")


class SafetyGuardResult:
    """Guard 체크 결과 모음. 다중 이슈 수집."""

    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.ok: bool = True

    def add(self, guard: str, severity: str, detail: dict[str, Any]) -> None:
        self.issues.append({
            "guard": guard,
            "severity": severity,
            "detail": detail,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
        })
        if severity in ("high", "critical"):
            self.ok = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "n_issues": len(self.issues),
            "issues": self.issues,
        }


class SafetyGuards:
    """S-2~S-6 안전 가드 통합 관리자.

    Hot Runner 사이클 전 check_all 호출 → SafetyGuardResult 기반 decision.
    S-6 emergency_halt은 Kill Switch 트리거 후 별도 청산 프로세스 필요 (Sprint 4+).
    """

    def __init__(
        self,
        auth_manager: AuthManager | None = None,
        kill_switch: KillSwitch | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self._auth = auth_manager
        self._kill = kill_switch
        self._audit = audit_logger

        cfg = _load_cfg()
        # yaml 필수 (fallback 제거, 불변 원칙 5)
        self._sanity_max_change_pct: float = float(cfg["sanity_max_change_pct"])
        self._reconciliation_tol_qty: int = int(cfg["reconciliation_tol_qty"])

        logger.info(
            "[safety_guards] 초기화: sanity_pct=%.2f, reconciliation_tol=%d",
            self._sanity_max_change_pct, self._reconciliation_tol_qty,
        )

    # ================================================================== #
    # S-2: OAuth 갱신 확인
    # ================================================================== #

    def check_oauth_refresh(self) -> dict[str, Any]:
        """AuthManager 토큰 만료 임박 여부 체크.

        Returns: {"guard": "S-2", "refresh_needed": bool, ...}
        """
        if self._auth is None:
            return {"guard": "S-2", "skipped": True, "reason": "no_auth_manager"}
        needs = self._auth.needs_refresh()   # public API 경유
        return {
            "guard": "S-2",
            "refresh_needed": bool(needs),
            "ts": datetime.now(tz=timezone.utc).isoformat(),
        }

    # ================================================================== #
    # S-3: Sanity Check: 가격 이상치
    # ================================================================== #

    def check_price_sanity(
        self,
        current_prices: dict[str, float],
        previous_prices: dict[str, float],
    ) -> list[dict[str, Any]]:
        """|current - previous| / previous > sanity_max_change_pct → 이상치.

        Returns: 이상치 리스트 [{"ticker", "prev", "current", "change_pct"}]
        """
        issues: list[dict[str, Any]] = []
        for ticker, curr in current_prices.items():
            padded = pad_ticker(str(ticker))
            prev = previous_prices.get(padded, previous_prices.get(str(ticker)))
            if prev is None or prev <= 0 or curr <= 0:
                continue
            change = abs(float(curr) - float(prev)) / float(prev)
            if change > self._sanity_max_change_pct:
                issues.append({
                    "ticker": padded,
                    "prev": float(prev),
                    "current": float(curr),
                    "change_pct": change,
                    "threshold": self._sanity_max_change_pct,
                })
        return issues

    # ================================================================== #
    # S-4: Reconciliation
    # ================================================================== #

    def reconcile_positions(
        self,
        system_positions: list[dict[str, Any]],
        kis_positions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """시스템 vs KIS 실제 포지션 qty 비교.

        각 포지션의 qty 차이가 tol 이상이면 mismatch.
        Returns: {"ok": bool, "mismatches": [...]}
        """
        sys_map = {
            pad_ticker(str(p["ticker"])): int(p.get("qty", 0))
            for p in system_positions if "ticker" in p
        }
        kis_map = {
            pad_ticker(str(p["ticker"])): int(p.get("qty", 0))
            for p in kis_positions if "ticker" in p
        }

        all_tickers = set(sys_map.keys()) | set(kis_map.keys())
        mismatches: list[dict[str, Any]] = []
        for t in sorted(all_tickers):
            sys_q = sys_map.get(t, 0)
            kis_q = kis_map.get(t, 0)
            diff = abs(sys_q - kis_q)
            if diff > self._reconciliation_tol_qty:
                mismatches.append({
                    "ticker": t,
                    "system_qty": sys_q,
                    "kis_qty": kis_q,
                    "diff": diff,
                })
        return {
            "ok": len(mismatches) == 0,
            "n_mismatches": len(mismatches),
            "mismatches": mismatches,
        }

    # ================================================================== #
    # S-5: Audit log (audit_logger wrapper)
    # ================================================================== #

    def audit(self, event_type: str, payload: dict[str, Any]) -> bool:
        """AuditLogger에 이벤트 기록. audit_logger=None이면 False."""
        if self._audit is None:
            return False
        try:
            self._audit.log(event_type, payload)
            return True
        except Exception as e:
            logger.warning("[safety_guards] audit 실패: %s", e)
            return False

    # ================================================================== #
    # S-6: Emergency halt
    # ================================================================== #

    def emergency_halt(
        self,
        reason: str,
        auto_liquidate: bool = False,
    ) -> dict[str, Any]:
        """KillSwitch 트리거 + 선택적 전량 청산 명령 지시.

        Sprint 1: KillSwitch trigger만. 전량 청산은 S4-6 Paper Trading 이후 실구현.
        Returns: 실행 결과 dict.
        """
        if self._kill is None:
            logger.warning("[safety_guards] emergency_halt 호출되었으나 kill_switch=None")
            return {"guard": "S-6", "triggered": False, "reason": "no_kill_switch"}

        self._kill.trigger(f"emergency_halt: {reason}")
        self.audit("emergency_halt", {
            "reason": reason,
            "auto_liquidate_requested": auto_liquidate,
            "note": "전량 청산은 Sprint 4+ 실구현 예정",
        })
        return {
            "guard": "S-6",
            "triggered": True,
            "reason": reason,
            "auto_liquidate": auto_liquidate,
            "note": "Sprint 1 MVP: KillSwitch 활성화만. 실 청산은 S4-6 이후.",
        }

    # ================================================================== #
    # 통합 체크
    # ================================================================== #

    def check_all(
        self,
        current_prices: dict[str, float] | None = None,
        previous_prices: dict[str, float] | None = None,
        system_positions: list[dict[str, Any]] | None = None,
        kis_positions: list[dict[str, Any]] | None = None,
        daily_pnl: float | None = None,
    ) -> SafetyGuardResult:
        """Hot Runner 1분 루프 전 호출 권장. 다중 guard 일괄 검증.

        각 guard 실패 시 SafetyGuardResult.issues에 추가.
        Critical/high 심각도 있으면 ok=False.
        """
        result = SafetyGuardResult()

        # S-1: KillSwitch daily_pnl 체크
        if self._kill and daily_pnl is not None:
            triggered = self._kill.check_daily_pnl(daily_pnl)
            if triggered:
                result.add("S-1", "critical", {
                    "daily_pnl": daily_pnl,
                    "threshold": self._kill.threshold,
                })

        # S-2: OAuth 갱신
        oauth_check = self.check_oauth_refresh()
        if oauth_check.get("refresh_needed"):
            result.add("S-2", "medium", {"refresh_needed": True})

        # S-3: 가격 이상치
        if current_prices and previous_prices:
            anomalies = self.check_price_sanity(current_prices, previous_prices)
            if anomalies:
                result.add("S-3", "high", {"n_anomalies": len(anomalies), "anomalies": anomalies})

        # S-4: Reconciliation
        if system_positions is not None and kis_positions is not None:
            recon = self.reconcile_positions(system_positions, kis_positions)
            if not recon["ok"]:
                result.add("S-4", "high", recon)

        # S-5: audit log 자동 기록 (결과 요약)
        self.audit("safety_guards_check", result.to_dict())

        return result
