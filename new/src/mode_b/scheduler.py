"""S3-0 Mode B Scheduler (C14 ModeBSchedulerContract).

Sprint 3 실구현. 18:00~22:00 KST 7단계 오케스트레이션.
Stage 내부 로직(Alpha Factor Engine, Co-STEER 등)은 Sprint 3 S3-1+ 에서 실구현.
현재 각 stage는 기반 인프라(bundle_id, 상태 전이, audit_log, timeout)가 실동작.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.ops.state_machine import PipelineState, StateMachine
from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_bundle_id
from src.utils.logger import get_logger
from src.utils.mode_guard import mode_b_only
from src.utils.safe_cast import safe_bool
from src.utils.trading_calendar import load_kospi_holidays

logger = get_logger("mode_b_scheduler")
_KST = ZoneInfo("Asia/Seoul")

# C14 forbidden_permissions 4개 (api_contracts.md C14 sub_components.forbidden_permissions SSOT)
# shared_message_pool_publish_during_market_hours / production_direct_write 2개는
# C12 BacktestAgentContract의 forbidden_permissions에 속함. C14에는 4개만.
FORBIDDEN_PERMISSIONS: frozenset[str] = frozenset({
    "hot_path_intervention",
    "fda_bypass",
    "target_weights_modification",
    "order_deltas_generation",
})


class PermissionViolationError(RuntimeError):
    """C14 FORBIDDEN_PERMISSIONS 런타임 위반. Mode B Scheduler 단계에서 금지 권한 사용 시도."""

    def __init__(self, permission: str) -> None:
        self.permission = permission
        super().__init__(
            f"[FORBIDDEN_PERMISSION] '{permission}'은 C14 Mode B Scheduler에서 금지된 권한. "
            "불변 원칙 3: Backtest Agent/Scheduler는 장중 경로에 개입 불가."
        )


def _check_permission(permission: str) -> None:
    """permission이 FORBIDDEN_PERMISSIONS에 포함되면 PermissionViolationError raise."""
    if permission in FORBIDDEN_PERMISSIONS:
        raise PermissionViolationError(permission)


class StageTimeoutError(RuntimeError):
    """Stage SLA 초과."""


class ModeBScheduler:
    """C14 ModeBSchedulerContract. 18:00~22:00 7단계 오케스트레이션.

    bundle_id: stage_3 시작 시 generate_bundle_id()로 발급.
    state_machine: 상태 전이 담당 (optional, 미주입 시 로그만).
    deployer: stage_7 배포 담당 (optional, 미주입 시 stub 반환).
    artifacts/mode_b_audit_log.jsonl: 매 stage 완료 시 append.

    불변 원칙 3: C14 FORBIDDEN_PERMISSIONS 4개는 어떤 stage에서도 실행 금지.
                 런타임 체크: _check_permission() 호출로 강제.
    불변 원칙 4: Mode B는 GPT-4o 전용. 장중 Kanana-o 예산 미소모.
    """

    def __init__(
        self,
        state_machine: StateMachine | None = None,
        deployer: Any = None,
        llm_router: Any = None,
        backtest_agent: Any = None,
    ) -> None:
        cfg = config_load("risk_config.yaml", "mode_b_scheduler") or {}
        self._stage_timeouts: dict[str, int] = cfg.get("stage_timeouts", {})
        audit_path_str = cfg.get("audit_log_path", "artifacts/mode_b_audit_log.jsonl")
        self._audit_log_path = Path(audit_path_str)
        self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_machine = state_machine
        self._deployer = deployer
        self._llm_router = llm_router
        self._backtest_agent = backtest_agent
        self._bundle_id: str | None = None
        self._current_verdict: str | None = None
        self._current_regression_risk: dict[str, Any] | None = None
        self._current_sanity_check_result: str = "fail"
        self._current_feature_quality: dict[str, Any] | None = None
        self._current_service_policy_evidence: dict[str, Any] | None = None
        logger.info("[mode_b_scheduler] 초기화 완료. audit_log=%s", self._audit_log_path)

    # ================================================================== #
    # Public API
    # ================================================================== #

    @mode_b_only
    def run_pipeline(self, date: str | None = None) -> dict[str, Any]:
        """C14 Mode B 파이프라인 실행 (S4-5: DQR stage_0 추가 → 총 8단계).

        stage_0: DQR (데이터 품질 확인). critical 커넥터 CRITICAL alert 시 차단.
        stage_1~7: 기존 7단계 (performance_analysis → deploy).
        bundle_id + stages 결과 반환.
        """
        date_str = date or datetime.now(_KST).strftime("%Y-%m-%d")
        logger.info("[mode_b_scheduler] Mode B 파이프라인 시작: %s", date_str)

        # IDLE → EVOLVING
        self._transition(PipelineState.MODE_B_EVOLVING)
        self._bundle_id = None
        self._current_verdict = None
        self._current_regression_risk = None
        self._current_sanity_check_result = "fail"
        self._current_feature_quality = None
        self._current_service_policy_evidence = None

        # Stage 0: DQR (S4-5). 데이터 품질 확인 후 나머지 진행.
        # critical 커넥터에서 CRITICAL alert 발생 시 파이프라인 차단.
        s0 = self._run_stage(
            "stage_0",
            lambda: self.stage_0_dqr(date_str),
            self._stage_timeouts.get("stage_0", 120),
        )
        if s0.get("critical_alert"):
            logger.error(
                "[mode_b_scheduler] DQR CRITICAL alert 감지. Mode B 파이프라인 차단: %s",
                s0.get("critical_connectors"),
            )
            self._transition(PipelineState.MODE_B_BLOCKED)
            self._transition(PipelineState.MODE_B_IDLE)
            return {
                "bundle_id": self._bundle_id,
                "date": date_str,
                "stages": [s0],
                "verdict": "blocked_dqr_critical",
                "dqr_alerts": s0.get("alerts", []),
            }

        # Stage 1~5 (EVOLVING phase)
        s1 = self._run_stage(
            "stage_1",
            self.stage_1_performance_analysis,
            self._stage_timeouts.get("stage_1", 30),
        )
        if self._stage_blocked(s1):
            return self._finish_blocked_pipeline(date_str, [s0, s1], s1)
        s2 = self._run_stage(
            "stage_2",
            self.stage_2_direction_selection,
            self._stage_timeouts.get("stage_2", 60),
        )
        if self._stage_blocked(s2):
            return self._finish_blocked_pipeline(date_str, [s0, s1, s2], s2)
        s3 = self._run_stage(
            "stage_3",
            self.stage_3_factor_evolution,
            self._stage_timeouts.get("stage_3", 3600),
        )
        if self._stage_blocked(s3):
            return self._finish_blocked_pipeline(date_str, [s0, s1, s2, s3], s3)
        s4 = self._run_stage(
            "stage_4",
            self.stage_4_model_evolution,
            self._stage_timeouts.get("stage_4", 1800),
        )
        if self._stage_blocked(s4):
            return self._finish_blocked_pipeline(date_str, [s0, s1, s2, s3, s4], s4)
        s5 = self._run_stage(
            "stage_5",
            self.stage_5_agent_self_improvement,
            self._stage_timeouts.get("stage_5", 1800),
        )
        if self._stage_blocked(s5):
            return self._finish_blocked_pipeline(date_str, [s0, s1, s2, s3, s4, s5], s5)

        # C14 backtest_skip_condition: candidate 없으면 Backtest 건너뜀
        has_candidates = bool(
            s3.get("factor_candidates")
            or s4.get("model_candidates")
            or s4.get("allocator_candidates")
        )

        if not has_candidates:
            logger.warning(
                "[mode_b_scheduler] candidate 없음. Backtest 건너뜀 (baseline 유지)"
            )
            self._transition(PipelineState.MODE_B_IDLE)
            return {
                "bundle_id": self._bundle_id,
                "date": date_str,
                "stages": [s0, s1, s2, s3, s4, s5],
                "verdict": "skipped_no_candidates",
            }

        # EVOLVING → BACKTEST
        self._transition(PipelineState.MODE_B_BACKTEST)
        s6 = self._run_stage(
            "stage_6",
            self.stage_6_backtest_validation,
            self._stage_timeouts.get("stage_6", 1800),
        )
        verdict = s6.get("verdict", "blocked")
        self._current_verdict = verdict

        # BACKTEST → DEPLOY / OPERATOR_REVIEW / BLOCKED
        if verdict == "pass" and not s6.get("critical_alert"):
            self._transition(PipelineState.MODE_B_DEPLOY)
            s7 = self._run_stage(
                "stage_7",
                self.stage_7_deploy,
                self._stage_timeouts.get("stage_7", 900),
            )
        elif verdict == "pass":
            self._transition(PipelineState.MODE_B_BLOCKED)
            s7 = {"stage": "stage_7", "status": "skipped_gate_blocked",
                  "verdict": verdict, "duration_sec": 0.0,
                  "error": "critical_alert_after_backtest"}
            self._append_audit_log({
                "timestamp": datetime.now(_KST).isoformat(),
                "stage": "stage_7", "duration_sec": 0.0,
                "bundle_id": self._bundle_id, "verdict": verdict,
                "regression_severity": s6.get("regression_severity"),
                "deploy_result": "skipped", "operator_approval": None,
                "error": "critical_alert_after_backtest",
            })
        elif verdict == "warn":
            self._transition(PipelineState.MODE_B_OPERATOR_REVIEW)
            s7 = {"stage": "stage_7", "status": "awaiting_operator_approval",
                  "verdict": verdict, "duration_sec": 0.0, "error": None}
            self._append_audit_log({
                "timestamp": datetime.now(_KST).isoformat(),
                "stage": "stage_7", "duration_sec": 0.0,
                "bundle_id": self._bundle_id, "verdict": verdict,
                "regression_severity": None, "deploy_result": None,
                "operator_approval": "pending", "error": None,
            })
        else:
            self._transition(PipelineState.MODE_B_BLOCKED)
            s7 = {"stage": "stage_7", "status": "skipped_verdict_blocked",
                  "verdict": verdict, "duration_sec": 0.0, "error": None}
            self._append_audit_log({
                "timestamp": datetime.now(_KST).isoformat(),
                "stage": "stage_7", "duration_sec": 0.0,
                "bundle_id": self._bundle_id, "verdict": verdict,
                "regression_severity": None, "deploy_result": "skipped",
                "operator_approval": None, "error": None,
            })

        deploy_status = self._classify_deploy_status(s7)
        pipeline_status = self._classify_pipeline_status(verdict, s6, s7, deploy_status)

        # → IDLE
        if verdict in ("pass", "blocked"):
            self._transition(PipelineState.MODE_B_IDLE)

        result = {
            "bundle_id": self._bundle_id,
            "date": date_str,
            "stages": [s0, s1, s2, s3, s4, s5, s6, s7],
            "verdict": verdict,
            "pipeline_status": pipeline_status,
            "deploy_status": deploy_status,
            "deploy_result": s7.get("deploy_result"),
        }
        logger.info(
            "[mode_b_scheduler] Mode B 파이프라인 완료: verdict=%s, bundle_id=%s",
            verdict,
            self._bundle_id,
        )
        return result

    @mode_b_only
    def approve_operator_review(self, bundle_id: str, approved: bool) -> dict[str, Any]:
        """Operator 승인/거부 후 IDLE 복귀. warn verdict 경로 전용.

        C14 OPERATOR_REVIEW → DEPLOY | BLOCKED → IDLE 전이 트리거.
        approved=True → MODE_B_DEPLOY → IDLE (배포는 별도 stage_7 재실행 필요).
        approved=False → MODE_B_BLOCKED → IDLE.
        """
        target_state = PipelineState.MODE_B_DEPLOY if approved else PipelineState.MODE_B_BLOCKED
        self._transition(target_state)
        self._transition(PipelineState.MODE_B_IDLE)
        self._append_audit_log({
            "timestamp": datetime.now(_KST).isoformat(),
            "stage": "operator_review_result",
            "duration_sec": 0.0,
            "bundle_id": bundle_id,
            "verdict": "approved" if approved else "rejected",
            "regression_severity": None,
            "deploy_result": None,
            "operator_approval": "approved" if approved else "rejected",
            "error": None,
        })
        logger.info("[mode_b_scheduler] operator_review bundle_id=%s approved=%s", bundle_id, approved)
        return {"bundle_id": bundle_id, "approved": approved, "state": "MODE_B_IDLE"}

    @mode_b_only
    def get_status(self, bundle_id: str) -> dict[str, Any]:
        """bundle_id 기준 audit_log에서 현재 상태 조회."""
        if not self._audit_log_path.exists():
            return {"bundle_id": bundle_id, "stages": [], "found": False}
        entries = []
        with self._audit_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("bundle_id") == bundle_id:
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
        return {"bundle_id": bundle_id, "stages": entries, "found": bool(entries)}

    # ================================================================== #
    # Stage implementations (stub: 각 Sprint에서 실구현)
    # ================================================================== #

    def _load_kospi_holidays(self, year: int) -> set:
        """risk_config.yaml dqr.kospi_holidays_{year} 로드. 날짜 set 반환.

        섹션 또는 year 키 없으면 빈 set.
        skip_on_holiday=false이면 빈 set 반환.
        """
        return load_kospi_holidays(year)

    def stage_0_dqr(self, date: str) -> dict[str, Any]:
        """S4-5 DQR (Data Quality Report). Mode B 첫 단계. 18:00 KST 이후 실행.

        DQRRunner.run_daily()로 8 커넥터 품질 측정 → 리포트 저장.
        critical 커넥터에서 CRITICAL severity alert 발생 시 critical_alert=True 반환.
        호출자(run_pipeline)가 critical_alert=True 시 파이프라인 차단.

        KRX 비거래일(주말/공휴일): DQR skip + critical_alert=False 반환.
        인프라 오류(Exception): critical_alert=True 반환 → 파이프라인 차단 (데이터 품질 미검증 방지).

        불변 원칙 5: 임계값은 DQRRunner가 risk_config.yaml dqr에서 로드.
        """
        logger.info("[mode_b_scheduler] stage_0 DQR 실행: date=%s", date)

        # KRX 비거래일 체크 (주말 + 공휴일)
        from datetime import date as _date_cls
        try:
            d = _date_cls.fromisoformat(date)
        except ValueError as e:
            logger.error("[mode_b_scheduler] DQR date 파싱 실패: %s error=%s", date, e)
            return {
                "status": "error",
                "dqr_date": date,
                "critical_alert": True,
                "alerts": [],
                "error": f"date 파싱 실패: {e}",
            }

        # 토요일(5) / 일요일(6)
        if d.weekday() >= 5:
            logger.info("[mode_b_scheduler] stage_0 DQR skip: %s (주말)", date)
            return {
                "status": "skipped",
                "reason": "weekend",
                "dqr_date": date,
                "critical_alert": False,
                "alerts": [],
            }

        # KRX 공휴일
        holidays = self._load_kospi_holidays(d.year)
        if d in holidays:
            logger.info("[mode_b_scheduler] stage_0 DQR skip: %s (공휴일)", date)
            return {
                "status": "skipped",
                "reason": "holiday",
                "dqr_date": date,
                "critical_alert": False,
                "alerts": [],
            }

        try:
            from src.dqr.dqr_runner import DQRRunner

            runner = DQRRunner()
            _test_pit_skip = os.getenv("ELEPHANT_TEST_PIT_SKIP") == "1"
            report = runner.run_daily(date=date, skip_pit_guard=_test_pit_skip)
            output_path = runner.save_report(report)
            alerts = report.get("alerts", [])
            critical_alerts = [a for a in alerts if a.get("severity") == "CRITICAL"]
            critical_connectors = list({a["connector"] for a in critical_alerts})
            logger.info(
                "[mode_b_scheduler] DQR 완료: alerts=%d critical=%d saved=%s",
                len(alerts),
                len(critical_alerts),
                output_path,
            )
            return {
                "status": "done",
                "dqr_date": date,
                "alert_count": len(alerts),
                "critical_alert": bool(critical_alerts),
                "critical_connectors": critical_connectors,
                "alerts": alerts,
                "report_path": str(output_path),
            }
        except Exception as e:
            logger.error("[mode_b_scheduler] DQR stage_0 인프라 오류: %s", e)
            # 인프라 오류 = 데이터 품질 검증 불가 → critical_alert=True로 파이프라인 차단
            return {
                "status": "error",
                "dqr_date": date,
                "critical_alert": True,
                "alerts": [],
                "error": str(e),
            }

    def stage_1_performance_analysis(self) -> dict[str, Any]:
        """§8.1 L2 성과 분석. ModeBPerformanceAggregator 연동은 S2-10에서 완비."""
        logger.info("[mode_b_scheduler] stage_1 성과 분석 실행")
        return {"status": "stub", "performance_summary": {}}

    def stage_2_direction_selection(self) -> dict[str, Any]:
        """§8.2 Co-STEER Thompson Sampling 방향 결정."""
        logger.info("[mode_b_scheduler] stage_2 방향 결정 실행")
        try:
            from src.mode_b.co_steer import CoSteer

            co = CoSteer(llm_router=self._llm_router)
            direction = co.select_direction()
            posteriors = co.posteriors()
            logger.info("[mode_b_scheduler] 방향 선택 완료: %s", direction)
            return {
                "status": "done",
                "direction": direction,
                "selected_strategies": [direction],
                "thompson_posteriors": posteriors,
            }
        except Exception as e:
            logger.warning(
                "[mode_b_scheduler] CoSteer 오류: %s. 기본값 factor_evolution", e
            )
            return {
                "status": "fallback",
                "direction": "factor_evolution",
                "selected_strategies": ["factor_evolution"],
            }

    def stage_3_factor_evolution(self) -> dict[str, Any]:
        """§8.3 Alpha Factor Engine. bundle_id 발급 시점. IdeaAgent 연동 (S3-1).

        IdeaAgent: GPT-4o 4요소 가설 생성. evolving anchor: 이전 성공 가설 기반 진화.
        LLMRouter는 scheduler 생성 시 self._llm_router 로 주입. 없으면 fallback.
        S3-2/S3-3에서 factor_candidates를 AST + IC 평가로 확장 예정.
        """
        self._bundle_id = generate_bundle_id()
        logger.info(
            "[mode_b_scheduler] stage_3 팩터 진화 실행, bundle_id=%s", self._bundle_id
        )

        # IdeaAgent 연동 (S3-1)
        hypotheses = []
        try:
            from src.mode_b.alpha_factor.idea_agent import IdeaAgent

            idea = IdeaAgent(llm_router=self._llm_router)
            market_ctx: dict[str, Any] = {
                "date": datetime.now(_KST).strftime("%Y-%m-%d"),
                "news_summary": "Mode B 자동 생성",
                "risk_level": "medium",
                "sector_moves": "N/A",
                "macro_signals": "N/A",
            }
            anchors = idea.load_latest_hypotheses(n=1)
            hypotheses = idea.generate_batch(
                market_ctx,
                anchors=anchors if anchors else None,
            )
        except Exception as e:
            logger.warning("[mode_b_scheduler] IdeaAgent 오류: %s. 빈 hypotheses 반환", e)

        # FactorAgent 연동 (S3-2): 각 가설에 대해 factor 코드 생성
        hypothesis_by_id: dict[str, Any] = {}
        factor_candidates_out: list[Any] = []
        try:
            from src.mode_b.alpha_factor.factor_agent import FactorAgent

            factor_a = FactorAgent(llm_router=self._llm_router)
            for h in hypotheses:
                hypothesis_by_id[h.hypothesis_id] = h
                candidate = factor_a.implement(h)
                factor_candidates_out.append(candidate)
        except Exception as e:
            logger.warning("[mode_b_scheduler] FactorAgent 오류: %s. 가설만 반환", e)
            factor_candidates_out = []

        # EvalAgent 연동 (S3-3): 각 후보 3중 정규화 평가
        factor_candidates: list[dict[str, Any]] = []
        evaluated: list[dict[str, Any]] = []
        try:
            from src.mode_b.alpha_factor.eval_agent import EvalAgent
            from src.mode_b.alpha_factor.factor_zoo import FactorZoo

            eval_a = EvalAgent(llm_router=self._llm_router)
            factor_zoo = [entry.to_dict() for entry in FactorZoo().list_by_status("active")]
            for candidate in factor_candidates_out:
                h = hypothesis_by_id.get(candidate.hypothesis_id)
                if h is None:
                    logger.warning(
                        "[mode_b_scheduler] hypothesis_id=%s 미발견. 평가 스킵",
                        candidate.hypothesis_id,
                    )
                    item: dict[str, Any] = {"candidate": candidate.to_dict(), "eval": None}
                    factor_candidates.append(item)
                    evaluated.append(item)
                    continue
                eval_result = eval_a.evaluate(candidate, h, factor_zoo=factor_zoo)
                entry: dict[str, Any] = {
                    "candidate": candidate.to_dict(),
                    "eval": eval_result.to_dict(),
                }
                factor_candidates.append(entry)
                evaluated.append(entry)
                logger.info(
                    "[mode_b_scheduler] EvalAgent: %s passed=%s r_g=%.3f",
                    candidate.candidate_id,
                    eval_result.passed,
                    eval_result.r_g,
                )
        except Exception as e:
            logger.warning("[mode_b_scheduler] EvalAgent 오류: %s. 평가 없이 반환", e)
            factor_candidates = [
                {"candidate": c.to_dict(), "eval": None} for c in factor_candidates_out
            ]
            evaluated = factor_candidates

        # FactorZoo 연동 (S3-4): 통과한 팩터를 Factor Zoo에 저장
        try:
            from src.mode_b.alpha_factor.eval_agent import EvalResult
            from src.mode_b.alpha_factor.factor_agent import FactorCandidate
            from src.mode_b.alpha_factor.factor_zoo import FactorZoo

            zoo = FactorZoo()
            zoo_candidates: list[str] = []
            for item in evaluated:
                eval_dict = item.get("eval")
                if eval_dict and eval_dict.get("passed"):
                    cand_dict = item["candidate"]
                    try:
                        cand = FactorCandidate(
                            **{k: cand_dict[k] for k in FactorCandidate.__dataclass_fields__}
                        )
                        hyp = hypothesis_by_id.get(cand.hypothesis_id)
                        eval_obj = EvalResult(**eval_dict) if eval_dict else None
                        zoo_entry = zoo.add_candidate(cand, hyp, eval_obj)
                        zoo_candidates.append(zoo_entry.candidate_id)
                    except Exception as inner_e:
                        logger.warning(
                            "[mode_b_scheduler] FactorZoo 단건 저장 실패 %s: %s",
                            cand_dict.get("candidate_id"),
                            inner_e,
                        )
            logger.info(
                "[mode_b_scheduler] FactorZoo 저장 완료: %d건", len(zoo_candidates)
            )
        except Exception as e:
            logger.warning("[mode_b_scheduler] FactorZoo 저장 오류: %s", e)

        return {
            "status": "done" if factor_candidates else "stub",
            "bundle_id": self._bundle_id,
            "factor_candidates": factor_candidates,
        }

    def stage_4_model_evolution(self) -> dict[str, Any]:
        """§8.4 LightGBM + PPO 야간 재학습. S3-6/S3-7 통합.

        NightlyLGBMRetrainer: FactorZoo active 팩터 포함 sliding window retrain.
        NightlyPPORetrainer: AllocationEnv + stable-baselines3 PPO 재학습.
        둘 중 하나 실패 시 graceful degradation (나머지 계속).

        불변 원칙 3: forbidden_permissions 위반 없음 (target_weights 수정 안 함).
        """
        logger.info("[mode_b_scheduler] stage_4 모델 진화 실행 (LightGBM + PPO)")
        model_candidates: list[dict[str, Any]] = []
        allocator_candidates: list[dict[str, Any]] = []
        stage_status = "done"
        lgbm_ok = False
        ppo_ok = False

        # LightGBM 재학습 (S3-6)
        lgbm_version = None
        lgbm_metrics: dict[str, Any] = {}
        try:
            from src.mode_b.nightly_lgbm_retrainer import NightlyLGBMRetrainer

            lgbm_result = NightlyLGBMRetrainer().retrain(bundle_id=self._bundle_id)
            lgbm_version = lgbm_result.get("version", "unknown")
            lgbm_path = lgbm_result.get("model_path", "")
            lgbm_metrics = lgbm_result.get("metrics", {})
            alpha_factors_used = lgbm_result.get("alpha_factors_used", 0)
            model_candidates.append({
                "version": lgbm_version,
                "path": lgbm_path,
                "metrics": lgbm_metrics,
                "alpha_factors_used": alpha_factors_used,
            })
            lgbm_ok = True
            logger.info(
                "[mode_b_scheduler] LightGBM 재학습 완료: version=%s alpha_factors=%d",
                lgbm_version,
                alpha_factors_used,
            )
        except Exception as e:
            logger.warning("[mode_b_scheduler] NightlyLGBMRetrainer 오류: %s", e)
            stage_status = "partial"

        # PPO 재학습 (S3-7)
        ppo_version = None
        try:
            from src.mode_b.nightly_ppo_retrainer import NightlyPPORetrainer

            ppo_result = NightlyPPORetrainer().retrain(bundle_id=self._bundle_id)
            ppo_version = ppo_result.get("version", "unknown")
            ppo_path = ppo_result.get("model_path", "")
            allocator_cand = ppo_result.get("allocator_candidate", {})
            if allocator_cand:
                allocator_candidates.append(allocator_cand)
            ppo_ok = True
            logger.info(
                "[mode_b_scheduler] PPO 재학습 완료: version=%s path=%s",
                ppo_version,
                ppo_path,
            )
        except Exception as e:
            logger.warning("[mode_b_scheduler] NightlyPPORetrainer 오류: %s", e)
            stage_status = "partial"

        if not model_candidates and not allocator_candidates:
            stage_status = "error"

        # Thompson Sampling posterior 업데이트 (CoSteer model_evolution arm)
        try:
            from src.mode_b.co_steer import CoSteer

            reward = 1.0 if (lgbm_ok and ppo_ok) else (0.5 if (lgbm_ok or ppo_ok) else 0.0)
            CoSteer(llm_router=self._llm_router).update_reward("model_evolution", reward)
            logger.info("[mode_b_scheduler] CoSteer model_evolution reward=%.1f", reward)
        except Exception as e:
            logger.warning("[mode_b_scheduler] CoSteer reward 업데이트 실패: %s", e)

        return {
            "status": stage_status,
            "lgbm_version": lgbm_version,
            "ppo_version": ppo_version,
            "model_candidates": model_candidates,
            "allocator_candidates": allocator_candidates,
            "metrics": lgbm_metrics,
        }

    def stage_5_agent_self_improvement(self) -> dict[str, Any]:
        """§8.5 에이전트 자기 개선. S3-11 KB 연동 시 실구현."""
        logger.info("[mode_b_scheduler] stage_5 자기 개선 실행")
        return {"status": "stub"}

    def stage_6_backtest_validation(self) -> dict[str, Any]:
        """§8.5.2 Backtest Agent 검증 게이트. BacktestAgent.run() 실호출."""
        logger.info("[mode_b_scheduler] stage_6 백테스트 검증 실행")

        bundle_id = self._bundle_id or "BUNDLE-UNKNOWN"

        # BacktestAgent 의존성: 미주입이면 기본 인스턴스 생성 (불변 원칙 3 준수)
        agent = self._backtest_agent
        if agent is None:
            try:
                from src.agents.mode_b.backtest import BacktestAgent
                agent = BacktestAgent()
            except Exception as e:
                logger.error("[mode_b_scheduler] BacktestAgent 인스턴스 생성 실패: %s", e)
                return {
                    "status": "error",
                    "verdict": "fail",
                    "regression_severity": "high",
                    "critical_alert": True,
                    "error": str(e),
                }

        # deploy_decision_gate 임계값 로드 (불변 원칙 5: yaml SSOT)
        gate_cfg = config_load("risk_config.yaml", "backtest_agent.deploy_decision_gate") or {}
        severity_block = gate_cfg.get("regression_severity_block", "high")

        try:
            bt_result = agent.run(bundle_id)
        except Exception as e:
            logger.error("[mode_b_scheduler] BacktestAgent.run() 실패: %s", e)
            return {
                "status": "error",
                "verdict": "fail",
                "regression_severity": "high",
                "critical_alert": True,
                "error": str(e),
                "bundle_id": bundle_id,
            }

        verdict = bt_result.get("verdict", "fail")
        regression_severity = bt_result.get("regression_severity", "high")
        regression_risk = bt_result.get("regression_risk") or {
            "flagged": regression_severity in ("medium", "high"),
            "severity": regression_severity if regression_severity in ("low", "medium", "high") else "low",
            "evidence": [],
        }
        leakage_check = bt_result.get("minute_bar_leakage_check") or {
            "verdict": "fail",
            "leakage_detected": True,
        }
        feature_quality = bt_result.get("feature_quality")
        if not isinstance(feature_quality, dict):
            feature_quality = {}
        service_policy_evidence = bt_result.get("service_policy_replay")
        if not isinstance(service_policy_evidence, dict):
            service_policy_evidence = {}
        sanity_check_result = (
            "ok" if leakage_check.get("verdict") == "pass" else "leakage_check_failed"
        )

        # regression_risk.flagged=True 이면 C14 deploy gate에서 배포 금지.
        severity_order = {"none": 0, "low": 1, "medium": 2, "high": 3}
        sev_level = severity_order.get(regression_severity, 3)
        block_level = severity_order.get(severity_block, 2)
        risk_flagged = safe_bool(regression_risk.get("flagged"), default=False)
        critical_alert = (
            verdict == "fail"
            or sanity_check_result != "ok"
            or risk_flagged
            or sev_level >= block_level
        )
        self._current_regression_risk = regression_risk
        self._current_sanity_check_result = sanity_check_result
        self._current_feature_quality = feature_quality
        self._current_service_policy_evidence = service_policy_evidence

        logger.info(
            "[mode_b_scheduler] stage_6 완료. bundle_id=%s verdict=%s severity=%s sanity=%s critical_alert=%s",
            bundle_id, verdict, regression_severity, sanity_check_result, critical_alert,
        )

        return {
            "status": "ok",
            "verdict": verdict,
            "regression_severity": regression_severity,
            "regression_risk": regression_risk,
            "minute_bar_leakage_check": leakage_check,
            "feature_quality": feature_quality,
            "service_policy_replay": service_policy_evidence,
            "sanity_check_result": sanity_check_result,
            "backtest_id": bt_result.get("backtest_id"),
            "metrics": bt_result.get("metrics", {}),
            "critical_alert": critical_alert,
            "bundle_id": bundle_id,
        }

    def stage_7_deploy(self) -> dict[str, Any]:
        """§8.6 22:00 배포. ModeBDeployer 경유. S3-10에서 실구현.

        Codex 권고 5 (2026-05-09 fix): ModeBDeployer.deploy() 시그니처 정합.
        이전: deploy(self._bundle_id) 단일 인자 → TypeError 재현.
        현재: deploy(bundle_id, backtest_verdict, sanity_check_result) 3 인자.
        verdict 는 stage_6 종료 시 self._current_verdict 에 저장된 값 사용.
        sanity_check_result 는 BacktestEngine.minute_bar_leakage_check + smoke_test
        통합 결과 (현재 stub: "ok" default. Sprint 4+ 실 검증).
        """
        logger.info(
            "[mode_b_scheduler] stage_7 배포 실행, bundle_id=%s verdict=%s",
            self._bundle_id, self._current_verdict,
        )
        if self._deployer:
            missing_evidence = []
            if not self._current_feature_quality:
                missing_evidence.append("feature_quality")
            if not self._current_service_policy_evidence:
                missing_evidence.append("service_policy_replay")
            if missing_evidence:
                return {
                    "status": "blocked",
                    "deploy_status": "blocked",
                    "deploy_result": "c12_deploy_evidence_missing",
                    "bundle_id": self._bundle_id,
                    "missing_evidence": missing_evidence,
                    "sanity_check_result": self._current_sanity_check_result,
                    "regression_risk": self._current_regression_risk or {},
                    "error": "c12_deploy_evidence_missing",
                }
            try:
                from src.mode_b.deployer import RegressionRisk

                risk = self._current_regression_risk or {}
                return self._deployer.deploy(
                    bundle_id=self._bundle_id or "BUNDLE-UNKNOWN",
                    backtest_verdict=self._current_verdict or "fail",
                    sanity_check_result=self._current_sanity_check_result,
                    regression_risk=RegressionRisk(
                        flagged=safe_bool(risk.get("flagged", False), default=False),
                        severity=str(risk.get("severity", "low")),
                        evidence=[str(e) for e in risk.get("evidence", [])],
                    ),
                    feature_quality=self._current_feature_quality,
                    service_policy_evidence=self._current_service_policy_evidence,
                )
            except NotImplementedError:
                pass
        return {
            "status": "blocked",
            "deploy_status": "not_configured",
            "deploy_result": "stub_no_deployer",
            "bundle_id": self._bundle_id,
            "sanity_check_result": self._current_sanity_check_result,
            "regression_risk": self._current_regression_risk or {},
            "error": "deployer_not_configured",
        }

    @staticmethod
    def _classify_deploy_status(stage_7: dict[str, Any]) -> str:
        """stage_7 결과를 BE-safe deploy_status enum으로 정규화."""
        if stage_7.get("deploy_status"):
            return str(stage_7["deploy_status"])
        if stage_7.get("error"):
            return "failed"
        deploy_result = stage_7.get("deploy_result")
        if deploy_result in {"deployed", "success", "ok"}:
            return "deployed"
        if deploy_result == "stub_no_deployer":
            return "not_configured"
        if str(stage_7.get("status", "")).startswith("skipped"):
            return "skipped"
        if stage_7.get("status") == "awaiting_operator_approval":
            return "awaiting_operator_approval"
        return "unknown"

    @staticmethod
    def _classify_pipeline_status(
        verdict: str,
        stage_6: dict[str, Any],
        stage_7: dict[str, Any],
        deploy_status: str,
    ) -> str:
        """C12 verdict와 C14 deploy 결과를 분리해 top-level 실행 상태를 반환."""
        if stage_6.get("error") or stage_7.get("error"):
            return "failed"
        if verdict == "warn" or deploy_status == "awaiting_operator_approval":
            return "awaiting_operator_approval"
        if verdict != "pass":
            return "blocked"
        if stage_6.get("critical_alert"):
            return "blocked"
        if deploy_status == "deployed":
            return "deployed"
        return "blocked"

    @staticmethod
    def _stage_blocked(stage: dict[str, Any]) -> bool:
        """Fail closed before C12/C14 when a pre-deploy stage is unhealthy."""
        status = str(stage.get("status", "")).lower()
        return (
            status in {"timeout", "error", "blocked"}
            or safe_bool(stage.get("critical_alert"), default=False)
            or safe_bool(stage.get("error"), default=False)
        )

    def _finish_blocked_pipeline(
        self,
        date_str: str,
        stages: list[dict[str, Any]],
        blocked_stage: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a fail-closed pipeline result after stage_1~5 failure."""
        stage_name = str(blocked_stage.get("stage", "unknown"))
        reason = blocked_stage.get("error") or blocked_stage.get("status") or "stage_blocked"
        logger.error(
            "[mode_b_scheduler] %s blocked before C12/deploy. reason=%s",
            stage_name,
            reason,
        )
        self._transition(PipelineState.MODE_B_BLOCKED)
        self._transition(PipelineState.MODE_B_IDLE)
        return {
            "bundle_id": self._bundle_id,
            "date": date_str,
            "stages": stages,
            "verdict": "blocked_stage_failure",
            "pipeline_status": "blocked",
            "deploy_status": "skipped",
            "deploy_result": "skipped_predeploy_stage_blocked",
            "blocked_stage": stage_name,
            "error": str(reason),
        }

    # ================================================================== #
    # Internal helpers
    # ================================================================== #

    # C14 stage별 사용 권한 매핑 (permitted 권한만 명시. 이 외 권한은 FORBIDDEN_PERMISSIONS 체크 대상)
    # 어떤 stage도 FORBIDDEN_PERMISSIONS 4개를 사용하지 않음을 런타임에 강제.
    _STAGE_REQUIRED_PERMISSIONS: dict[str, frozenset[str]] = {
        "stage_0": frozenset({"read_connector_stats", "dqr_write"}),   # S4-5 DQR
        "stage_1": frozenset({"read_performance_history"}),
        "stage_2": frozenset({"read_thompson_posteriors", "write_thompson_posteriors"}),
        "stage_3": frozenset({"llm_call_mode_b", "factor_zoo_write"}),
        "stage_4": frozenset({"llm_call_mode_b", "model_registry_write"}),
        "stage_5": frozenset({"llm_call_mode_b"}),
        "stage_6": frozenset({"read_backtest_results"}),
        "stage_7": frozenset({"deploy_artifacts_write"}),
    }

    def _run_stage(
        self,
        stage_name: str,
        stage_fn: Any,
        timeout_sec: int,
    ) -> dict[str, Any]:
        """단일 stage 실행 + audit_log 기록 + duration 측정 + timeout 경고.

        진입 시 FORBIDDEN_PERMISSIONS 런타임 체크:
          stage가 선언한 required_permissions 중 FORBIDDEN_PERMISSIONS에 포함된 것이 있으면
          즉시 PermissionViolationError raise. 불변 원칙 3 강제.
        """
        # FORBIDDEN_PERMISSIONS 런타임 강제 (불변 원칙 3)
        required = self._STAGE_REQUIRED_PERMISSIONS.get(stage_name, frozenset())
        for perm in required:
            if perm in FORBIDDEN_PERMISSIONS:
                raise PermissionViolationError(perm)
        logger.debug(
            "[mode_b_scheduler] %s 권한 체크 통과. required=%d forbidden_check=OK",
            stage_name,
            len(required),
        )

        t0 = time.perf_counter()
        error: str | None = None
        result: dict[str, Any] = {}
        timed_out = False
        if stage_name == "stage_7":
            try:
                result = stage_fn() or {}
            except Exception as e:
                error = str(e)
                logger.error("[mode_b_scheduler] %s 오류: %s", stage_name, e)
            duration = round(time.perf_counter() - t0, 3)
            result.setdefault("timeout_policy", "synchronous_no_thread_timeout")
            stage_error = error if error is not None else result.get("error")
            log_entry = {
                "timestamp": datetime.now(_KST).isoformat(),
                "stage": stage_name,
                "duration_sec": duration,
                "bundle_id": self._bundle_id,
                "verdict": result.get("verdict"),
                "regression_severity": result.get("regression_severity"),
                "deploy_result": result.get("deploy_result"),
                "operator_approval": result.get("operator_approval"),
                "error": stage_error,
            }
            self._append_audit_log(log_entry)
            return {**result, "stage": stage_name, "duration_sec": duration, "error": stage_error}

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(stage_fn)
        try:
            result = future.result(timeout=timeout_sec) or {}
        except concurrent.futures.TimeoutError:
            timed_out = True
            error = f"StageTimeoutError: {stage_name} exceeded {timeout_sec}s"
            result = {
                "status": "timeout",
                "verdict": "blocked",
                "critical_alert": True,
                "timeout_sec": timeout_sec,
                "timeout_warning": True,
                "timeout_policy": "fail_closed_no_thread_hard_cancel",
                "timeout_hard_cancelled": False,
            }
            future.cancel()
            logger.error("[mode_b_scheduler] %s", error)
        except Exception as e:
            error = str(e)
            logger.error("[mode_b_scheduler] %s 오류: %s", stage_name, e)
        finally:
            # ThreadPoolExecutor cannot hard-kill a running Python function.
            # On timeout, return a blocked stage result immediately and record
            # timeout_hard_cancelled=False so demo/pass reports cannot hide it.
            executor.shutdown(wait=not timed_out, cancel_futures=True)

        duration = round(time.perf_counter() - t0, 3)
        stage_error = error if error is not None else result.get("error")

        log_entry = {
            "timestamp": datetime.now(_KST).isoformat(),
            "stage": stage_name,
            "duration_sec": duration,
            "bundle_id": self._bundle_id,
            "verdict": result.get("verdict"),
            "regression_severity": result.get("regression_severity"),
            "deploy_result": result.get("deploy_result"),
            "operator_approval": result.get("operator_approval"),
            "error": stage_error,
        }
        self._append_audit_log(log_entry)
        return {**result, "stage": stage_name, "duration_sec": duration, "error": stage_error}

    def _append_audit_log(self, entry: dict[str, Any]) -> None:
        with self._audit_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _transition(self, target: PipelineState) -> None:
        if self._state_machine:
            try:
                self._state_machine.transition(target)
            except Exception as e:
                logger.warning(
                    "[mode_b_scheduler] 상태 전이 실패 %s: %s", target.value, e
                )
        logger.info("[mode_b_scheduler] 상태 전이 → %s", target.value)
