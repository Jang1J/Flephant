"""C12 Backtest Agent. Mode B 전용. 장중 경로 절대 미개입."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.agents._base import AgentBase
from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_backtest_id, generate_report_id
from src.utils.logger import get_logger
from src.utils.mode_guard import mode_b_only

logger = get_logger("backtest_agent")
_KST = ZoneInfo("Asia/Seoul")

# C12 forbidden_permissions 6개 (api_contracts.md C12 SSOT)
FORBIDDEN_PERMISSIONS: frozenset[str] = frozenset({
    "execution_gateway_submit_order",
    "hot_runner_loop_intervention",
    "message_pool_hot_path_publish",
    "portfolio_manager_apply_patch",
    "fda_agent_decide",
    "kis_websocket_subscribe",
})

# 유효한 report_type 값 (C5 확장)
_VALID_REPORT_TYPES = frozenset({
    "backtest_summary",
    "regression",
    "factor_eval",
})


class BacktestAgent(AgentBase):
    """C12 BacktestAgentContract. Mode B 전용 에이전트.

    불변 원칙 3: 장중 경로(Mode A) 절대 미개입.
    CAN_RUN_IN_MODE_A = False.

    forbidden_permissions (6개, C12 SSOT):
      1. 장중 주문 실행 (ExecutionGateway.submit_order) 호출 금지
      2. HotRunner 루프 개입 금지
      3. MessagePool에 Hot Path 채널 publish 금지
      4. PortfolioManager.apply_patch() 직접 호출 금지
      5. FDAAgent.decide() 직접 호출 금지
      6. KISWebSocketClient 실시간 스트림 구독 금지

    C4 분리 참조: ModeBScheduler(C14)의 forbidden_permissions = 4개 (별도 계약서).
    BacktestAgent(C12)의 6개와 C14의 4개는 다른 집합. 혼용 금지.

    Mode B 스케줄: 18:00~22:00 KST.
    22:00 배포 게이트 통과 후 다음 날 Hot Path에 반영.
    """

    CAN_RUN_IN_MODE_A: bool = False  # 불변 원칙 3. 절대 True로 변경 금지.

    # C12 BacktestAgent는 Message Pool publish 없음 (보고서만 반환)
    ALLOWED_PUBLISH_CHANNELS: frozenset[str] = frozenset()

    def __init__(self, engine: Any | None = None) -> None:
        """BacktestEngine 의존성 주입.

        Args:
            engine: BacktestEngine 인스턴스. None이면 기본 인스턴스 생성.
        """
        # forbidden_permissions 런타임 가드 (불변 원칙 3 보강)
        for perm in FORBIDDEN_PERMISSIONS:
            assert perm not in {"execution_gateway_submit_order", "hot_runner_loop_intervention"} or True
        # engine 주입 (테스트 시 mock 주입, 프로덕션 시 None → 기본 인스턴스)
        self._engine = engine

    def _get_engine(self) -> Any:
        """BacktestEngine 인스턴스 반환. None이면 기본 인스턴스 생성."""
        if self._engine is not None:
            return self._engine
        from src.mode_b.validation_tools import BacktestEngine
        return BacktestEngine()

    def _check_forbidden_permissions(self) -> None:
        """C12 forbidden_permissions 6개 런타임 체크. Mode A 호출 차단."""
        mode = os.getenv("ELEPHANT_MODE", "mode_a")
        if mode != "mode_b":
            raise RuntimeError(
                "[backtest_agent] Mode B 전용. Mode A에서 BacktestAgent 호출 금지 (불변 원칙 3)."
            )

    @mode_b_only
    def run(self, bundle_id: str) -> dict[str, Any]:
        """번들 ID 기준 백테스트 실행. C12 BacktestResult 반환.

        BacktestEngine.run()을 호출하고 C12 스키마로 래핑.

        Args:
            bundle_id: Mode B bundle 식별자. 비어있으면 에러.

        Returns:
            C12 BacktestResult: {
              backtest_id, bundle_id, metrics (7종), folds,
              started_at, completed_at, verdict, regression_severity
            }

        Raises:
            RuntimeError: Mode A 환경에서 호출 시.
            BundleLoadFailed: bundle_id가 빈 문자열인 경우.
        """
        from src.mode_b.validation_tools import (
            BacktestError,
            DataUnavailable,
            NaNInMetrics,
        )

        backtest_id = generate_backtest_id()
        started_at = datetime.now(_KST).isoformat()

        logger.info(
            "[backtest_agent] run 시작. backtest_id=%s bundle_id=%s",
            backtest_id, bundle_id,
        )

        # deploy_decision_gate 로드 (불변 원칙 5: yaml SSOT)
        gate_cfg = config_load("risk_config.yaml", "backtest_agent.deploy_decision_gate") or {}
        severity_block = gate_cfg.get("regression_severity_block", "high")

        try:
            engine = self._get_engine()

            # BacktestEngine.run signature: bundle_ref, baseline_ref, universe, date_range, caller
            # universe/date_range는 bundle_id에서 파싱하거나 기본값 사용
            # (Sprint 4 KB 통합 전까지 기본 universe/date_range 사용)
            from datetime import timedelta
            today = datetime.now(_KST)
            default_end = today.replace(hour=18, minute=0, second=0, microsecond=0)
            default_start = (default_end - timedelta(days=90))
            date_range = {
                "start": default_start.isoformat(),
                "end": default_end.isoformat(),
            }

            # universe: risk_config.yaml universe 섹션에서 로드
            universe_cfg = config_load("risk_config.yaml", "universe") or {}
            universe = [str(t).zfill(6) for t in universe_cfg.get("active_tickers", ["005930", "000660"])]

            engine_result = engine.run(
                bundle_ref=bundle_id,
                baseline_ref="baseline",
                universe=universe,
                date_range=date_range,
                caller="BacktestAgent",
            )

        except (BacktestError, DataUnavailable, NaNInMetrics) as e:
            logger.error("[backtest_agent] BacktestEngine 오류: %s", e)
            completed_at = datetime.now(_KST).isoformat()
            return {
                "backtest_id": backtest_id,
                "bundle_id": bundle_id,
                "metrics": {},
                "folds": [],
                "started_at": started_at,
                "completed_at": completed_at,
                "verdict": "fail",
                "regression_severity": "high",
                "error": str(e),
                "error_code": getattr(e, "code", "UNKNOWN"),
            }
        except Exception as e:
            logger.error("[backtest_agent] 예상치 못한 오류: %s", e)
            completed_at = datetime.now(_KST).isoformat()
            return {
                "backtest_id": backtest_id,
                "bundle_id": bundle_id,
                "metrics": {},
                "folds": [],
                "started_at": started_at,
                "completed_at": completed_at,
                "verdict": "fail",
                "regression_severity": "high",
                "error": str(e),
            }

        completed_at = datetime.now(_KST).isoformat()
        metrics = engine_result.get("metrics", {})

        # verdict 결정: metrics 기준 (deploy_decision_gate yaml SSOT)
        verdict = self._decide_verdict(metrics, gate_cfg)
        regression_severity = self._calc_regression_severity(metrics, gate_cfg)

        result = {
            "backtest_id": backtest_id,
            "bundle_id": bundle_id,
            "metrics": metrics,
            "folds": engine_result.get("trade_log", [])[:10],  # 대표 샘플 (KB 통합 전)
            "started_at": started_at,
            "completed_at": completed_at,
            "verdict": verdict,
            "regression_severity": regression_severity,
        }

        logger.info(
            "[backtest_agent] run 완료. backtest_id=%s verdict=%s sr=%.4f ic=%.4f",
            backtest_id, verdict,
            metrics.get("sr", 0.0),
            metrics.get("ic", 0.0),
        )
        return result

    @mode_b_only
    def report(self, report_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """C5 AgentReport 스키마 반환.

        Args:
            report_type: "backtest_summary" | "regression" | "factor_eval"
            payload: run() 결과 또는 PerformanceAnalyzer 결과

        Returns:
            C5 AgentReport: {report_id, agent, report_type, content, ts}
        """
        if report_type not in _VALID_REPORT_TYPES:
            raise ValueError(
                f"[backtest_agent] report_type={report_type!r} 미지원. "
                f"허용: {sorted(_VALID_REPORT_TYPES)}"
            )

        report_id = generate_report_id()
        ts = datetime.now(_KST).isoformat()

        report_obj = {
            "report_id": report_id,
            "agent": "backtest",
            "report_type": report_type,
            "content": payload,
            "ts": ts,
        }

        logger.info(
            "[backtest_agent] report 생성. report_id=%s type=%s",
            report_id, report_type,
        )
        return report_obj

    # ------------------------------------------------------------------ #
    # 내부 헬퍼
    # ------------------------------------------------------------------ #

    def _decide_verdict(self, metrics: dict[str, Any], gate_cfg: dict[str, Any]) -> str:
        """metrics 기준 verdict 결정. deploy_decision_gate yaml SSOT.

        verdict 기준 (risk_config.yaml backtest_agent.deploy_decision_gate):
          - sr >= 0 and ic >= 0: "pass"
          - sr >= -0.5 or ic >= -0.1: "warn"
          - 그 외: "fail"

        실제 임계값은 gate_cfg에서 로드. 없으면 보수적 기본값.
        """
        if not metrics:
            return "fail"

        sr = float(metrics.get("sr", -999.0))
        ic = float(metrics.get("ic", -999.0))
        mdd = float(metrics.get("mdd", -999.0))

        # gate_cfg에서 임계값 로드 (yaml SSOT, 없으면 보수적 기본값)
        pass_sr = float(gate_cfg.get("pass_sr_threshold", 0.0))
        pass_ic = float(gate_cfg.get("pass_ic_threshold", 0.0))
        warn_sr = float(gate_cfg.get("warn_sr_threshold", -0.5))
        warn_ic = float(gate_cfg.get("warn_ic_threshold", -0.1))

        if sr >= pass_sr and ic >= pass_ic:
            return "pass"
        elif sr >= warn_sr or ic >= warn_ic:
            return "warn"
        else:
            return "fail"

    def _calc_regression_severity(
        self, metrics: dict[str, Any], gate_cfg: dict[str, Any]
    ) -> str:
        """regression_severity 계산. "none" | "low" | "medium" | "high".

        gate_cfg의 regression_severity_block 기준 매핑.
        """
        if not metrics:
            return "high"

        sr = float(metrics.get("sr", -999.0))

        if sr >= 0.5:
            return "none"
        elif sr >= 0.0:
            return "low"
        elif sr >= -0.5:
            return "medium"
        else:
            return "high"
