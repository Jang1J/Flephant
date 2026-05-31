"""C12 Backtest Agent. Mode B 전용. 장중 경로 절대 미개입."""
from __future__ import annotations

import math
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.agents._base import AgentBase
from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_backtest_id, generate_report_id
from src.utils.logger import get_logger
from src.utils.mode_guard import mode_b_only
from src.utils.safe_cast import safe_bool, safe_float, safe_int

logger = get_logger("backtest_agent")
_KST = ZoneInfo("Asia/Seoul")

# C12 forbidden_permissions 6개 (api_contracts.md C12 + risk_config.yaml SSOT)
# SHIP-2 (W1 SSOT cleanup): 코드 명칭 → 계약서 명칭 통일.
# 코드 synonym 매핑 (구 → 신):
#   execution_gateway_submit_order  → order_deltas_generation
#   hot_runner_loop_intervention    → hot_path_intervention
#   message_pool_hot_path_publish   → shared_message_pool_publish_during_market_hours
#   portfolio_manager_apply_patch   → target_weights_modification
#   fda_agent_decide                → fda_bypass
#   kis_websocket_subscribe         → production_direct_write
FORBIDDEN_PERMISSIONS: frozenset[str] = frozenset({
    "target_weights_modification",
    "order_deltas_generation",
    "fda_bypass",
    "hot_path_intervention",
    "shared_message_pool_publish_during_market_hours",
    "production_direct_write",
})

# 유효한 report_type 값 (C5 확장)
_VALID_REPORT_TYPES = frozenset({
    "backtest_summary",
    "regression",
    "factor_eval",
})


def _empty_metrics() -> dict[str, float]:
    """C12 metrics 7종을 실패 경로에서도 항상 채운다."""
    return {
        "ic": 0.0,
        "icir": 0.0,
        "rank_ic": 0.0,
        "arr": 0.0,
        "ir": 0.0,
        "mdd": 0.0,
        "sr": 0.0,
    }


def _empty_daily_series() -> dict[str, Any]:
    """C12 daily-series fields are present even on fail-closed reports."""
    return {
        "initial_capital": 0.0,
        "daily_pnl": [],
        "daily_returns": [],
        "daily_equity": [],
    }


class BacktestAgent(AgentBase):
    """C12 BacktestAgentContract. Mode B 전용 에이전트.

    불변 원칙 3: 장중 경로(Mode A) 절대 미개입.
    CAN_RUN_IN_MODE_A = False.

    forbidden_permissions (6개, C12 + risk_config.yaml SSOT 명칭):
      1. target_weights_modification (PortfolioManager.apply_patch 등 weight 직접 수정)
      2. order_deltas_generation (ExecutionGateway.submit_order 등 주문 생성)
      3. fda_bypass (FDAAgent.decide() 우회 또는 직접 호출)
      4. hot_path_intervention (HotRunner 루프 개입)
      5. shared_message_pool_publish_during_market_hours (Hot 채널 publish)
      6. production_direct_write (KISWebSocketClient 등 운영 시스템 직접 쓰기)

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
        # engine 주입 (테스트 시 mock 주입, 프로덕션 시 None → 기본 인스턴스)
        self._engine = engine

    def _get_engine(self) -> Any:
        """BacktestEngine 인스턴스 반환. None이면 기본 인스턴스 생성."""
        if self._engine is not None:
            return self._engine
        from src.mode_b.validation_tools import BacktestEngine
        return BacktestEngine()

    @staticmethod
    def _normalize_float_series(values: Any) -> list[float]:
        if not isinstance(values, list):
            return []
        return [safe_float(value, default=0.0) for value in values]

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
        date_range: dict[str, str] = {}

        # P1 fix (2026-05-09): try 진입 전 _force_verdict_fail 초기화.
        # BaseException (KeyboardInterrupt 등) 발생 시에도 stale flag 차단. except 내 reset 보다 안전.
        self._force_verdict_fail = None

        try:
            engine = self._get_engine()

            # BacktestEngine.run signature: bundle_ref, baseline_ref, universe, date_range, caller
            # universe/date_range는 bundle_id에서 파싱하거나 기본값 사용
            # (Sprint 4 KB 통합 전까지 기본 universe/date_range 사용)
            from datetime import timedelta
            from src.utils.trading_calendar import kospi_trading_start_date
            # SHIP-fix C-4 (GPT Pro 2026-05-09): PIT-Safety guard.
            # 실행 시각이 snapshot_hour (18:00 KST) 이전이면 default_end 를 어제 18:00 으로 후퇴.
            # 18:00 이후 실행 시에만 오늘 18:00 까지 포함 (정상 Mode B 시점).
            now = datetime.now(_KST)
            pit_cfg = config_load("risk_config.yaml", "pit_safety") or {}
            snapshot_hour = int(pit_cfg.get("snapshot_hour", 18))
            today_snapshot = now.replace(hour=snapshot_hour, minute=0, second=0, microsecond=0)
            if now < today_snapshot:
                # 18:00 이전 실행: 오늘 18:00 은 미래 → 어제 18:00 으로 후퇴
                default_end = today_snapshot - timedelta(days=1)
                logger.warning(
                    "[backtest_agent] 실행 시각 (%s) 이 snapshot_hour (%d:00 KST) 이전. "
                    "default_end 를 어제 %d:00 KST 로 후퇴 (PIT-Safety).",
                    now.isoformat(), snapshot_hour, snapshot_hour
                )
            else:
                default_end = today_snapshot
            wf_cfg = config_load("risk_config.yaml", "walk_forward") or {}
            vt_cfg = config_load("risk_config.yaml", "validation_tools.backtest_engine") or {}
            trading_days_needed = (
                int(wf_cfg.get("train_window_days", 60))
                + int(math.ceil(int(vt_cfg.get("purge_bars", 60)) / 390))
                + int(math.ceil(int(vt_cfg.get("embargo_bars", 78)) / 390))
                + int(wf_cfg.get("test_window_days", 20))
                + max(0, int(wf_cfg.get("n_splits", 8)) - 1)
                * int(wf_cfg.get("step_days", wf_cfg.get("test_window_days", 20)))
            )
            default_start_date = kospi_trading_start_date(
                default_end.date(),
                trading_days_needed,
            )
            default_start = default_end.replace(
                year=default_start_date.year,
                month=default_start_date.month,
                day=default_start_date.day,
            )
            date_range = {
                "start": default_start.isoformat(),
                "end": default_end.isoformat(),
            }

            # SHIP-5 (W1 P0-3): universe SSOT = universe_config.yaml.
            # 최종 deploy gate는 risk_config.yaml final_dataset_gate의
            # allowed_*_statuses를 사용해 30종목(active + pending_data)을 검증한다.
            universe_cfg = config_load("universe_config.yaml") or {}
            final_dataset_gate = (
                gate_cfg.get("final_dataset_gate", {})
                if isinstance(gate_cfg.get("final_dataset_gate", {}), dict)
                else {}
            )
            allowed_stock_statuses = {
                str(status)
                for status in (
                    final_dataset_gate.get("allowed_stock_statuses")
                    or ["active"]
                )
            }
            allowed_sector_statuses = {
                str(status)
                for status in (
                    final_dataset_gate.get("allowed_sector_statuses")
                    or ["confirmed"]
                )
            }
            min_universe_tickers = safe_int(
                final_dataset_gate.get("min_tickers", 0),
                default=0,
                min_value=0,
            )
            sectors = universe_cfg.get("sectors", {})
            universe = []
            for sector_data in sectors.values():
                if str(sector_data.get("status")) not in allowed_sector_statuses:
                    continue
                for stock in sector_data.get("stocks", []):
                    if str(stock.get("status")) in allowed_stock_statuses:
                        universe.append(str(stock["ticker"]).zfill(6))
            if min_universe_tickers and len(set(universe)) < min_universe_tickers:
                raise BacktestError(
                    "universe_config.yaml final deploy universe is incomplete: "
                    f"{len(set(universe))}/{min_universe_tickers} tickers"
                )
            if not universe:
                # SHIP-fix W-4 (GPT Pro 2026-05-09): fallback 발동 = backtest 결과 신뢰도 0.
                # active/pending universe 검증 실패가 축소 백테스트로 숨는 위험 차단.
                # 1. fallback_tickers 도 비어있으면 즉시 BacktestError raise.
                # 2. fallback_tickers 가 있어도 verdict 강제 fail + operator alert.
                fallback_cfg = universe_cfg.get("backtest_universe_mode", {}).get("fallback_tickers", [])
                if not fallback_cfg:
                    raise BacktestError(
                        "universe_config.yaml SSOT 손상 의심: active 종목과 fallback_tickers 모두 비어있음. "
                        "operator 직접 yaml 점검 필요."
                    )
                universe = [str(t).zfill(6) for t in fallback_cfg]
                logger.error(
                    "[backtest_agent] CRITICAL: 20종목 active universe 검증 실패 → fallback_tickers (%d종목) 사용. "
                    "축소 백테스트 결과는 신뢰도 0. verdict 강제 fail 처리. operator yaml 점검 필수.",
                    len(universe),
                )
                # verdict 강제 fail flag (아래 verdict 결정 로직에서 우선 적용)
                self._force_verdict_fail = "universe_fallback_active"

            engine_result = engine.run(
                bundle_ref=bundle_id,
                baseline_ref="baseline",
                universe=universe,
                date_range=date_range,
                caller="BacktestAgent",
            )

        except (BacktestError, DataUnavailable, NaNInMetrics) as e:
            logger.error("[backtest_agent] BacktestEngine 오류: %s", e)
            # P1 fix (2026-05-09): except 진입 시 _force_verdict_fail stale flag reset.
            # 이전: engine.run() 예외 시 reset 누락 → 인스턴스 재사용 시 stale flag 오염.
            self._force_verdict_fail = None
            completed_at = datetime.now(_KST).isoformat()
            report = {
                "bundle_id": bundle_id,
                "backtest_id": backtest_id,
                "started_at": started_at,
                "finished_at": completed_at,
                "completed_at": completed_at,
                "date_range": date_range,
                "verdict": "fail",
                "metrics": _empty_metrics(),
                "regime_breakdown": [],
                "ablation": [],
                "regression_risk": {"flagged": True, "severity": "high",
                                     "evidence": [f"engine_error: {str(e)}"]},
                "regression_severity": "high",
                "diagnostic_notes": f"BacktestEngine error: {str(e)}",
                "llm_reasoning_ref": "",
                "failure_case_cards": [],
                "regression_cases": [],
                "minute_bar_leakage_check": self._normalize_leakage_check(
                    None, default_verdict="fail"
                ),
                "feature_quality": {},
                "folds": [],
                **_empty_daily_series(),
                "error": str(e),
                "error_code": getattr(e, "code", "UNKNOWN"),
            }
            return self._attach_service_policy_evidence(report)
        except Exception as e:
            logger.error("[backtest_agent] 예상치 못한 오류: %s", e)
            # P1 fix (2026-05-09): except 진입 시 _force_verdict_fail stale flag reset.
            self._force_verdict_fail = None
            completed_at = datetime.now(_KST).isoformat()
            report = {
                "bundle_id": bundle_id,
                "backtest_id": backtest_id,
                "started_at": started_at,
                "finished_at": completed_at,
                "completed_at": completed_at,
                "date_range": date_range,
                "verdict": "fail",
                "metrics": _empty_metrics(),
                "regime_breakdown": [],
                "ablation": [],
                "regression_risk": {"flagged": True, "severity": "high",
                                     "evidence": [f"engine_error: {str(e)}"]},
                "regression_severity": "high",
                "diagnostic_notes": f"BacktestEngine error: {str(e)}",
                "llm_reasoning_ref": "",
                "failure_case_cards": [],
                "regression_cases": [],
                "minute_bar_leakage_check": self._normalize_leakage_check(
                    None, default_verdict="fail"
                ),
                "feature_quality": {},
                "folds": [],
                **_empty_daily_series(),
                "error": str(e),
            }
            return self._attach_service_policy_evidence(report)

        completed_at = datetime.now(_KST).isoformat()
        metrics = engine_result.get("metrics", {})
        candidate_artifact = engine_result.get("candidate_artifact", {})
        candidate_model_metadata = (
            candidate_artifact.get("metadata")
            if isinstance(candidate_artifact, dict)
            and isinstance(candidate_artifact.get("metadata"), dict)
            else {}
        )
        artifact_gate_evidence: list[str] = []
        if isinstance(candidate_artifact, dict) and candidate_artifact.get("synthetic_fallback"):
            artifact_gate_evidence.append(
                "candidate_artifact_synthetic_fallback:"
                f"{candidate_artifact.get('source', 'unknown')}"
            )
            self._force_verdict_fail = "candidate_artifact_synthetic_fallback"

        # verdict 결정: metrics 기준 (deploy_decision_gate yaml SSOT)
        # SHIP-fix W-4: universe fallback 발동 시 verdict 강제 fail (축소 백테스트 신뢰도 0)
        force_fail_reason = getattr(self, "_force_verdict_fail", None)
        if force_fail_reason:
            verdict = "fail"
            self._force_verdict_fail = None  # reset for next run
        else:
            verdict = self._decide_verdict(metrics, gate_cfg)
        regression_severity = self._calc_regression_severity(metrics, gate_cfg)
        if force_fail_reason:
            regression_severity = "high"
            artifact_gate_evidence.append(str(force_fail_reason))
        if regression_severity == "high":
            verdict = "fail"

        # SHIP-fix C-3 (GPT Pro 2026-05-09): C12 backtest_report schema 정합.
        # api_contracts.md C12 output 필수 필드: bundle_id, backtest_id, started_at, finished_at,
        # verdict, metrics, regime_breakdown, ablation, regression_risk, diagnostic_notes,
        # minute_bar_leakage_check.
        # finished_at = completed_at alias 유지 (코드 호환).
        finished_at = completed_at
        regime_breakdown = engine_result.get("regime_breakdown", [])
        ablation = engine_result.get("ablation", [])

        # regression_risk: regression_severity 를 C12 schema 형태로 wrapping.
        # P1 fix (2026-05-09): C12 schema field 명 evidence (api_contracts.md L885).
        # 이전: details (drift). engine_result key 도 regression_evidence 로 통일.
        regression_risk = {
            "flagged": regression_severity in ("medium", "high"),
            "severity": regression_severity if regression_severity in ("low", "medium", "high") else "low",
            "evidence": self._normalize_evidence(
                engine_result.get(
                    "regression_evidence",
                    engine_result.get("regression_details", []),
                )
            ),
        }
        regression_risk["evidence"].extend(artifact_gate_evidence)

        # diagnostic_notes: GPT-4o LLM reasoning 결과 (현재 stub, Sprint 4+ 실 연동)
        diagnostic_notes = engine_result.get("diagnostic_notes",
                                              f"verdict={verdict} regression_severity={regression_severity}")
        if artifact_gate_evidence:
            diagnostic_notes = f"{diagnostic_notes}; artifact_gate={','.join(artifact_gate_evidence)}"

        minute_bar_leakage_check = self._normalize_leakage_check(
            engine_result.get("minute_bar_leakage_check"),
            default_verdict="pass",
        )
        if minute_bar_leakage_check["verdict"] != "pass":
            verdict = "fail"
            regression_risk["flagged"] = True
            regression_risk["severity"] = "high"
            regression_risk["evidence"].append("minute_bar_leakage_check_failed")

        result = {
            "bundle_id": bundle_id,
            "backtest_id": backtest_id,
            "started_at": started_at,
            "finished_at": finished_at,                    # C12 정합 (completed_at alias)
            "completed_at": completed_at,                   # backward compat (test 호환)
            "date_range": date_range,
            "verdict": verdict,
            "metrics": metrics,
            "regime_breakdown": regime_breakdown,
            "ablation": ablation,
            "regression_risk": regression_risk,             # C12 정합
            "regression_severity": regression_severity,     # backward compat
            "diagnostic_notes": diagnostic_notes,
            "llm_reasoning_ref": engine_result.get("llm_reasoning_ref", ""),
            "failure_case_cards": engine_result.get("failure_case_cards", []),
            "regression_cases": engine_result.get("regression_cases", []),
            "minute_bar_leakage_check": minute_bar_leakage_check,
            "feature_quality": engine_result.get("feature_quality", {}),
            "initial_capital": safe_float(
                engine_result.get("initial_capital"),
                default=0.0,
                min_value=0.0,
            ),
            "daily_pnl": self._normalize_float_series(engine_result.get("daily_pnl")),
            "daily_returns": self._normalize_float_series(
                engine_result.get("daily_returns")
            ),
            "daily_equity": self._normalize_float_series(
                engine_result.get("daily_equity")
            ),
            "service_policy_expected_date_range": engine_result.get(
                "service_policy_expected_date_range",
                {},
            ),
            "candidate_artifact": candidate_artifact,
            "candidate_model_metadata": candidate_model_metadata,
            "folds": engine_result.get("trade_log", [])[:10],  # 대표 샘플 (KB 통합 전)
        }

        logger.info(
            "[backtest_agent] run 완료. backtest_id=%s verdict=%s sr=%.4f ic=%.4f",
            backtest_id, verdict,
            metrics.get("sr", 0.0),
            metrics.get("ic", 0.0),
        )
        return self._attach_service_policy_evidence(result)

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

    @staticmethod
    def _normalize_evidence(raw: Any) -> list[str]:
        """C12 regression_risk.evidence를 list[str]로 정규화."""
        if raw is None:
            return []
        if isinstance(raw, list):
            return [str(item) for item in raw]
        if isinstance(raw, dict):
            return [f"{key}={value}" for key, value in raw.items()]
        return [str(raw)]

    @staticmethod
    def _attach_service_policy_evidence(report: dict[str, Any]) -> dict[str, Any]:
        """Attach explicit service-policy PASS/BLOCKED/MISSING schema."""
        from src.mode_b.backtest_diagnostics import attach_service_policy_evidence

        return attach_service_policy_evidence(
            report,
            expected_date_range=(
                report.get("service_policy_expected_date_range")
                or report.get("date_range")
            ),
        )

    @staticmethod
    def _normalize_leakage_check(
        raw: dict[str, Any] | None,
        default_verdict: str,
    ) -> dict[str, Any]:
        """C12 minute_bar_leakage_check schema 정규화."""
        cfg = config_load("risk_config.yaml", "validation_tools.backtest_engine") or {}
        raw = raw or {}
        verdict_raw = str(raw.get("verdict", default_verdict)).lower()
        verdict = "pass" if verdict_raw == "pass" else "fail"
        leakage_detected = safe_bool(raw.get("leakage_detected", verdict != "pass"))
        return {
            "purge_bars_used": safe_int(
                raw.get("purge_bars_used", raw.get("purge_bars", cfg.get("purge_bars", 60))),
                default=safe_int(cfg.get("purge_bars", 60), default=60),
                min_value=0,
            ),
            "embargo_bars_used": safe_int(
                raw.get("embargo_bars_used", raw.get("embargo_bars", cfg.get("embargo_bars", 78))),
                default=safe_int(cfg.get("embargo_bars", 78), default=78),
                min_value=0,
            ),
            "replay_unit": str(raw.get("replay_unit", cfg.get("replay_unit", "1m"))),
            "leakage_detected": leakage_detected,
            "verdict": verdict,
        }

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

        SHIP-fix NEW-1 (2026-05-06): SR 임계값 yaml SSOT (불변 5원칙 하드코딩 금지).
        gate_cfg = risk_config.yaml backtest_agent.deploy_decision_gate 참조.
        """
        if not metrics:
            return "high"

        sr = float(metrics.get("sr", -999.0))

        # severity 임계값 yaml SSOT (default: none>=0.5, low>=0.0, medium>=-0.5)
        sev_none = float(gate_cfg.get("severity_none_sr_threshold", 0.5))
        sev_low = float(gate_cfg.get("severity_low_sr_threshold", 0.0))
        sev_medium = float(gate_cfg.get("severity_medium_sr_threshold", -0.5))

        if sr >= sev_none:
            return "none"
        elif sr >= sev_low:
            return "low"
        elif sr >= sev_medium:
            return "medium"
        else:
            return "high"
