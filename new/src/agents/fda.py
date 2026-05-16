"""S1-4 FDA (Final Decision Agent) Hot Path + S2-9 Cold Path 구현.

C9 FDADecisionContract. 불변 원칙 2 집행:
  - CAN_CHANGE_WEIGHT = False (target_weights / order_deltas 수정 금지)
  - read-only echo만 output에 포함
  - approve/veto 양쪽 reason_code 필수 (cause-centric 설계)

Hot Path mode='hot' (비LLM, <10ms):
  규칙 기반 decide. dependency_status + quant anomaly + risk warning 조합.
Cold Path mode='cold' (Kanana-o CoT, S2-9 실구현):
  LLM CoT + 에이전트 신호 가중합 → approve/veto + reason_code.

reason_code enum (S2-9 완료, 최종 확정):
  NORMAL_APPROVE / TIMEOUT / RISK_FAST_TRIGGER / DEBATE_CONFLICT /
  NEWS_DIVERGENCE / QUANT_ANOMALY / MISSING_PORTFOLIO_PATCH
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from typing import Any

from src.agents._base import AgentBase
from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_decision_id
from src.utils.llm_parser import parse_llm_json
from src.utils.logger import get_logger
from src.utils.safe_cast import safe_float

logger = get_logger("fda")

_FDA_COLD_CALLER = "fda_cold_path"
_FDA_REQUIRED_REASON_CODES = frozenset({
    "NORMAL_APPROVE",
    "TIMEOUT",
    "RISK_FAST_TRIGGER",
    "DEBATE_CONFLICT",
    "NEWS_DIVERGENCE",
    "QUANT_ANOMALY",
    "MISSING_PORTFOLIO_PATCH",
})


class MissingPortfolioPatchError(ValueError):
    """C9 error: MISSING_PORTFOLIO_PATCH."""


class ExpiredDecisionError(ValueError):
    """C9 error: EXPIRED_DECISION."""


class IllegalDeltaModificationError(RuntimeError):
    """C9 error: ILLEGAL_DELTA_MODIFICATION_ATTEMPT."""


class MissingReasonCodeError(RuntimeError):
    """C9 error: MISSING_REASON_CODE."""


class FDAConfigError(RuntimeError):
    """FDA risk_config 로드/검증 실패. fail-closed."""


class FDAAgent(AgentBase):
    """C9 Final Decision Agent. approve/veto + reason_code 필수.

    불변 원칙 2: CAN_CHANGE_WEIGHT = False 클래스 레벨 상수 + 인스턴스 guard.
    target_weights / order_deltas는 read-only echo만 output에 포함. 수정 시도 시
    IllegalDeltaModificationError.
    """

    # 불변 원칙 2 enforcement. Sprint 0 contract test가 assert.
    CAN_CHANGE_WEIGHT: bool = False

    # FDA publishes: final_decision
    ALLOWED_PUBLISH_CHANNELS = frozenset({"final_decision"})

    def __init__(self, llm_router: Any | None = None) -> None:
        """
        llm_router: LLMRouter 인스턴스 (S2-9 Cold Path 전용).
                    None이면 Cold Path 호출 시 NEWS_DIVERGENCE fail-closed.
        """
        self._llm_router = llm_router
        # 설정 누락/오염 시 magic fallback 없이 초기화 단계에서 fail-closed.
        debate_cfg = self._require_config_section("debate")
        self._debate_uncertainty_threshold = self._require_probability(
            debate_cfg,
            section="debate",
            key="uncertainty_threshold",
        )

        unc_cfg = self._require_config_section("fda_uncertainty_link")
        self._uncertainty_threshold = self._require_probability(
            unc_cfg,
            section="fda_uncertainty_link",
            key="uncertainty_threshold",
        )
        self._veto_prior_boost = self._require_probability(
            unc_cfg,
            section="fda_uncertainty_link",
            key="veto_prior_boost",
        )

        self._valid_reason_codes = self._load_reason_code_catalog()

        logger.info(
            "[fda] 초기화: CAN_CHANGE_WEIGHT=%s, reason_codes=%d종, cold_path=%s",
            self.CAN_CHANGE_WEIGHT, len(self._valid_reason_codes),
            "enabled" if self._llm_router else "disabled_fail_closed",
        )

    @staticmethod
    def _require_config_section(section: str) -> dict[str, Any]:
        try:
            cfg = config_load("risk_config.yaml", section)
        except Exception as e:
            raise FDAConfigError(
                f"[fda] risk_config.yaml {section} 섹션 로드 실패. FDA fail-closed."
            ) from e
        if not isinstance(cfg, dict) or not cfg:
            raise FDAConfigError(
                f"[fda] risk_config.yaml {section} 섹션이 비어 있거나 dict가 아님. "
                "FDA fail-closed."
            )
        return cfg

    @staticmethod
    def _require_probability(cfg: dict[str, Any], *, section: str, key: str) -> float:
        if key not in cfg:
            raise FDAConfigError(
                f"[fda] risk_config.yaml {section}.{key} 누락. FDA fail-closed."
            )
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FDAConfigError(
                f"[fda] risk_config.yaml {section}.{key} 타입 오류: {type(value).__name__}. "
                "FDA fail-closed."
            )
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0.0 or numeric > 1.0:
            raise FDAConfigError(
                f"[fda] risk_config.yaml {section}.{key} 범위 오류: {value}. "
                "FDA fail-closed."
            )
        return numeric

    def _load_reason_code_catalog(self) -> frozenset[str]:
        rc_cfg = self._require_config_section("reason_code_catalog")
        if rc_cfg.get("status") != "final":
            raise FDAConfigError(
                "[fda] risk_config.yaml reason_code_catalog.status가 final이 아님. "
                "FDA fail-closed."
            )

        candidates = rc_cfg.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise FDAConfigError(
                "[fda] risk_config.yaml reason_code_catalog.candidates 오류. "
                "FDA fail-closed."
            )

        invalid = [
            c for c in candidates
            if not isinstance(c, str) or not c.strip()
        ]
        if invalid:
            raise FDAConfigError(
                "[fda] risk_config.yaml reason_code_catalog.candidates에 "
                f"비문자열/빈 값 존재: {invalid}. FDA fail-closed."
            )

        codes = frozenset(c.strip() for c in candidates)
        missing = _FDA_REQUIRED_REASON_CODES - codes
        if missing:
            raise FDAConfigError(
                "[fda] risk_config.yaml reason_code_catalog 후보 누락: "
                f"{sorted(missing)}. FDA fail-closed."
            )
        return codes

    # ================================================================== #
    # Public API
    # ================================================================== #

    def decide(
        self,
        portfolio_patch_ref: str | None,
        target_weights: dict[str, float] | None = None,
        order_deltas: list[dict[str, Any]] | None = None,
        dependency_status: dict[str, str] | None = None,
        active_reports: list[str] | None = None,
        anomalies: list[dict[str, Any]] | None = None,
        risk_warnings: list[dict[str, Any]] | None = None,
        mode: str = "hot",
        risk_fast_eval: dict[str, Any] | None = None,
        debate_result: dict[str, Any] | None = None,
        agent_signals: list[dict[str, Any]] | None = None,
        uncertainty_score: float = 0.0,
    ) -> dict[str, Any]:
        """C9 final_decision 생성.

        Hot Path (mode='hot') 비LLM 규칙 기반 (<10ms):
          1. portfolio_patch_ref 없음 → veto (MISSING_PORTFOLIO_PATCH)
          2. dependency_status 중 timeout → veto (TIMEOUT)
          3. risk_fast_eval risk_level=critical|high → veto (RISK_FAST_TRIGGER)
          4. risk_warnings severity=high → veto (RISK_FAST_TRIGGER)
          5. anomalies (QuantAgent intraday_drop 등) → veto (QUANT_ANOMALY)
          6. 그 외 → approve (NORMAL_APPROVE)

        Cold Path (mode='cold') Kanana-o CoT (S2-9 실구현):
          에이전트 신호 가중합 → approve/veto + reason_code.
          debate_result + agent_signals + risk_warnings 통합 판단.

        불변 원칙 2: FDA는 weight 변경 금지. risk_fast_eval / debate_result는
          reason_code 결정에만 활용.
        """
        t0 = time.perf_counter()

        if mode == "cold":
            result = self._decide_cold(
                portfolio_patch_ref=portfolio_patch_ref,
                target_weights=target_weights or {},
                order_deltas=order_deltas or [],
                risk_warnings=risk_warnings or [],
                debate_result=debate_result,
                agent_signals=agent_signals or [],
                active_reports=active_reports or [],
                t0=t0,
                uncertainty_score=float(uncertainty_score),
            )
            result["mode"] = "cold"  # Cold Path임을 C9 mode 필드에 명시
            return result

        # 1. portfolio_patch_ref 확인
        if not portfolio_patch_ref:
            return self._build_veto_decision(
                reason_code="MISSING_PORTFOLIO_PATCH",
                veto_reason="portfolio_patch_ref 누락. PM patch 선행 필요.",
                target_weights=target_weights or {},
                order_deltas=order_deltas or [],
                portfolio_patch_ref=str(portfolio_patch_ref or ""),
                t0=t0,
            )

        dependency_status = dependency_status or {}
        anomalies = anomalies or []
        risk_warnings = risk_warnings or []

        # 2. dependency timeout 체크
        # (risk_fast_eval은 step 3에서 처리)
        timeout_deps = [
            name for name, status in dependency_status.items()
            if str(status).strip().lower() == "timeout"
        ]
        if timeout_deps:
            return self._build_veto_decision(
                reason_code="TIMEOUT",
                veto_reason=f"dependency timeout: {sorted(timeout_deps)}",
                target_weights=target_weights or {},
                order_deltas=order_deltas or [],
                portfolio_patch_ref=portfolio_patch_ref,
                t0=t0,
                risk_overrides=[
                    {
                        "rule": "dependency_wait",
                        "original": "all_done",
                        "override": f"timeout_{name}",
                        "justification": "agent did not respond within SLA",
                    }
                    for name in timeout_deps
                ],
            )

        # 3. RiskFastAgent eval — critical|high → veto (불변 원칙 2: weight 수정 금지)
        if risk_fast_eval is not None:
            rf_level = str(risk_fast_eval.get("risk_level", "low")).lower()
            if rf_level in ("critical", "high"):
                triggered = risk_fast_eval.get("triggered_rules", [])
                affected = risk_fast_eval.get("affected_tickers", [])
                return self._build_veto_decision(
                    reason_code="RISK_FAST_TRIGGER",
                    veto_reason=(
                        f"RiskFast {rf_level}: rules={triggered}, "
                        f"tickers={affected}"
                    ),
                    target_weights=target_weights or {},
                    order_deltas=order_deltas or [],
                    portfolio_patch_ref=portfolio_patch_ref,
                    t0=t0,
                )

        # 4. Risk warnings severity=high → veto
        high_risks = [
            r for r in risk_warnings
            if str(r.get("severity", "")).lower() in ("high", "critical")
        ]
        if high_risks:
            tickers = [r.get("ticker", "") for r in high_risks]
            return self._build_veto_decision(
                reason_code="RISK_FAST_TRIGGER",
                veto_reason=f"high-severity risk: tickers={tickers}",
                target_weights=target_weights or {},
                order_deltas=order_deltas or [],
                portfolio_patch_ref=portfolio_patch_ref,
                t0=t0,
            )

        # 5. Quant anomaly → veto
        if anomalies:
            anomaly_types = [a.get("anomaly_type", "unknown") for a in anomalies]
            return self._build_veto_decision(
                reason_code="QUANT_ANOMALY",
                veto_reason=f"quant anomaly: {anomaly_types}",
                target_weights=target_weights or {},
                order_deltas=order_deltas or [],
                portfolio_patch_ref=portfolio_patch_ref,
                t0=t0,
            )

        # 6. Approve
        return self._build_approve_decision_hot(
            target_weights=target_weights or {},
            order_deltas=order_deltas or [],
            portfolio_patch_ref=portfolio_patch_ref,
            active_reports=active_reports or [],
            t0=t0,
        )

    # ================================================================== #
    # Internal: Decision builders
    # ================================================================== #

    def _build_approve_decision_hot(
        self,
        target_weights: dict[str, float],
        order_deltas: list[dict[str, Any]],
        portfolio_patch_ref: str,
        active_reports: list[str],
        t0: float,
    ) -> dict[str, Any]:
        """Hot Path approve 빌더 (reason_code 고정)."""
        return self._finalize_decision(
            approved=True,
            reason_code="NORMAL_APPROVE",
            veto_reason=None,
            target_weights=target_weights,
            order_deltas=order_deltas,
            portfolio_patch_ref=portfolio_patch_ref,
            t0=t0,
            active_reports=active_reports,
        )

    def _build_veto_decision(
        self,
        reason_code: str,
        veto_reason: str,
        target_weights: dict[str, float],
        order_deltas: list[dict[str, Any]],
        portfolio_patch_ref: str,
        t0: float,
        active_reports: list[str] | None = None,
        risk_overrides: list[dict[str, Any]] | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        return self._finalize_decision(
            approved=False,
            reason_code=reason_code,
            veto_reason=veto_reason,
            target_weights=target_weights,
            order_deltas=order_deltas,
            portfolio_patch_ref=portfolio_patch_ref,
            t0=t0,
            active_reports=active_reports,
            risk_overrides=risk_overrides,
            confidence=confidence,
        )

    def _finalize_decision(
        self,
        approved: bool,
        reason_code: str,
        veto_reason: str | None,
        target_weights: dict[str, float],
        order_deltas: list[dict[str, Any]],
        portfolio_patch_ref: str,
        t0: float,
        active_reports: list[str] | None = None,
        risk_overrides: list[dict[str, Any]] | None = None,
        mode: str = "hot",
        confidence: float | None = None,
    ) -> dict[str, Any]:
        # 불변 원칙 2: read-only echo 검증
        self._assert_readonly(target_weights, order_deltas)

        # reason_code 필수
        if not reason_code or reason_code not in self._valid_reason_codes:
            raise MissingReasonCodeError(
                f"[fda] reason_code={reason_code} invalid. "
                f"허용: {sorted(self._valid_reason_codes)}"
            )

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        final_confidence = (
            self._safe_confidence(confidence, default=1.0 if approved else 0.5)
            if confidence is not None
            else (1.0 if approved else 0.5)
        )

        return {
            "final_decision": {
                "decision_id": generate_decision_id(),
                "approved": bool(approved),
                "target_weights": dict(target_weights),      # echo
                "order_deltas": [dict(od) for od in order_deltas],  # echo
                "veto_reason": veto_reason,
                "reason_code": reason_code,
                "risk_overrides": risk_overrides or [],
                "confidence": final_confidence,
                "expiry": now_iso,                             # Hot Path는 즉시 유효
                "portfolio_patch_ref": portfolio_patch_ref,
                "active_reports": list(active_reports or []),
            },
            "mode": mode,
            "latency_ms": elapsed_ms,
        }

    def _assert_readonly(
        self,
        target_weights: dict[str, float],
        order_deltas: list[dict[str, Any]],
    ) -> None:
        """불변 원칙 2: FDA는 target_weights / order_deltas 수정 못 함.

        _finalize_decision이 원본 복사본을 만들므로 여기서는 input 무결성만 확인.
        dict / list 타입 외 수정 시도는 TypeError로 자연 거부됨.
        """
        if self.CAN_CHANGE_WEIGHT:
            raise IllegalDeltaModificationError(
                "CAN_CHANGE_WEIGHT=True 감지. 불변 원칙 2 위반. Boot-time guard."
            )
        # structural check
        if not isinstance(target_weights, dict):
            raise IllegalDeltaModificationError(
                f"target_weights must be dict (echo). got {type(target_weights)}"
            )
        if not isinstance(order_deltas, list):
            raise IllegalDeltaModificationError(
                f"order_deltas must be list (echo). got {type(order_deltas)}"
            )

    # ================================================================== #
    # S2-9: Cold Path
    # ================================================================== #

    def _decide_cold(
        self,
        portfolio_patch_ref: str | None,
        target_weights: dict[str, float],
        order_deltas: list[dict[str, Any]],
        risk_warnings: list[dict[str, Any]],
        debate_result: dict[str, Any] | None,
        agent_signals: list[dict[str, Any]],
        active_reports: list[str],
        t0: float,
        uncertainty_score: float = 0.0,
    ) -> dict[str, Any]:
        """Cold Path 판단. Kanana-o CoT + 에이전트 신호 가중합.

        불변 원칙 2 준수: weight 수정 없음. approve/veto + reason_code만 결정.
        """
        # 0. uncertainty_score 임계값 초과 → NEWS_DIVERGENCE veto (Dual-Source 연계)
        if uncertainty_score >= self._uncertainty_threshold:
            reason = (
                f"uncertainty_score={uncertainty_score:.3f} >= "
                f"threshold={self._uncertainty_threshold:.3f}"
            )
            logger.warning("[fda] Cold Path uncertainty veto. %s", reason)
            return self._build_veto_decision(
                reason_code="NEWS_DIVERGENCE",
                veto_reason=f"Dual-Source divergence 임계 초과. {reason}",
                target_weights=target_weights,
                order_deltas=order_deltas,
                portfolio_patch_ref=portfolio_patch_ref or "",
                t0=t0,
                active_reports=active_reports,
            )

        # 1. 기본 규칙 (Hot과 동일): portfolio_patch 없으면 veto
        if not portfolio_patch_ref:
            return self._build_veto_decision(
                reason_code="MISSING_PORTFOLIO_PATCH",
                veto_reason="Cold Path: portfolio_patch_ref 누락.",
                target_weights=target_weights,
                order_deltas=order_deltas,
                portfolio_patch_ref="",
                t0=t0,
                active_reports=active_reports,
            )

        # 2. debate_result: 충돌 있고 uncertainty_delta > 임계값 → DEBATE_CONFLICT veto
        if debate_result and debate_result.get("conflict_detected"):
            uncertainty = safe_float(
                debate_result.get("uncertainty_delta", 0.0),
                default=0.0,
                min_value=0.0,
                max_value=1.0,
            )
            if uncertainty > self._debate_uncertainty_threshold:
                return self._build_veto_decision(
                    reason_code="DEBATE_CONFLICT",
                    veto_reason=(
                        f"Debate 충돌 미해소. uncertainty_delta={uncertainty:.2f}. "
                        f"patterns={debate_result.get('skipped_reason', '')}"
                    ),
                    target_weights=target_weights,
                    order_deltas=order_deltas,
                    portfolio_patch_ref=portfolio_patch_ref,
                    t0=t0,
                    active_reports=active_reports,
                )

        # 3. risk_warnings 검토 (risk_warning stance=veto_recommendation)
        veto_signals = [
            r for r in risk_warnings
            if r.get("stance") == "veto_recommendation"
            or str(r.get("risk_level", "")).lower() in ("high", "critical")
        ]
        if veto_signals:
            return self._build_veto_decision(
                reason_code="RISK_FAST_TRIGGER",
                veto_reason=f"Cold Path risk_warning veto. count={len(veto_signals)}",
                target_weights=target_weights,
                order_deltas=order_deltas,
                portfolio_patch_ref=portfolio_patch_ref,
                t0=t0,
                active_reports=active_reports,
            )

        # 4. LLM CoT: router 미주입 또는 호출 실패 시 approve하지 않고 fail-closed
        if self._llm_router is None:
            logger.warning("[fda] Cold Path LLM router 없음. NEWS_DIVERGENCE fail-closed.")
            return self._build_veto_decision(
                reason_code="NEWS_DIVERGENCE",
                veto_reason="Cold Path LLM router 없음. FDA fail-closed.",
                target_weights=target_weights,
                order_deltas=order_deltas,
                portfolio_patch_ref=portfolio_patch_ref,
                t0=t0,
                active_reports=active_reports,
            )

        # 5. Kanana-o CoT 판단
        prompt = self._build_cold_prompt(
            agent_signals, risk_warnings, debate_result
        )
        llm_result = self._llm_router.call(
            prompt, mode="cold", caller=_FDA_COLD_CALLER
        )

        if not llm_result.success:
            logger.warning("[fda] Cold Path LLM 실패. NEWS_DIVERGENCE fail-closed.")
            return self._build_veto_decision(
                reason_code="NEWS_DIVERGENCE",
                veto_reason="Cold Path LLM 실패. FDA fail-closed.",
                target_weights=target_weights,
                order_deltas=order_deltas,
                portfolio_patch_ref=portfolio_patch_ref,
                t0=t0,
                active_reports=active_reports,
            )

        parsed = self._parse_cold_llm(llm_result.content)
        if parsed.get("approved") is True:
            return self._build_approve_decision(
                target_weights=target_weights,
                order_deltas=order_deltas,
                portfolio_patch_ref=portfolio_patch_ref,
                active_reports=active_reports,
                t0=t0,
                reason_code=parsed["reason_code"],
                confidence=parsed.get("confidence"),
            )
        else:
            return self._build_veto_decision(
                reason_code=parsed.get("reason_code", "NEWS_DIVERGENCE"),
                veto_reason=parsed.get("veto_reason", "Cold Path CoT veto."),
                target_weights=target_weights,
                order_deltas=order_deltas,
                portfolio_patch_ref=portfolio_patch_ref,
                t0=t0,
                active_reports=active_reports,
                confidence=parsed.get("confidence"),
            )

    def _build_approve_decision(
        self,
        target_weights: dict[str, float],
        order_deltas: list[dict[str, Any]],
        portfolio_patch_ref: str,
        active_reports: list[str],
        t0: float,
        reason_code: str = "NORMAL_APPROVE",
        confidence: float | None = None,
    ) -> dict[str, Any]:
        return self._finalize_decision(
            approved=True,
            reason_code=reason_code,
            veto_reason=None,
            target_weights=target_weights,
            order_deltas=order_deltas,
            portfolio_patch_ref=portfolio_patch_ref,
            t0=t0,
            active_reports=active_reports,
            confidence=confidence,
        )

    def _build_cold_prompt(
        self,
        agent_signals: list[dict[str, Any]],
        risk_warnings: list[dict[str, Any]],
        debate_result: dict[str, Any] | None,
    ) -> str:
        """Cold Path FDA CoT 프롬프트 구성."""
        signals_str = json.dumps(
            [{"channel": s.get("channel"), "payload": s.get("payload")} for s in agent_signals[:5]],
            ensure_ascii=False,
        )
        risk_str = json.dumps(
            [{"stance": r.get("stance"), "risk_level": r.get("risk_level")} for r in risk_warnings[:3]],
            ensure_ascii=False,
        )
        debate_str = ""
        if debate_result:
            uncertainty = safe_float(
                debate_result.get("uncertainty_delta", 0),
                default=0.0,
                min_value=0.0,
                max_value=1.0,
            )
            debate_str = (
                f"\n토론 결과: winner={debate_result.get('winner_view')}, "
                f"uncertainty={uncertainty:.2f}"
            )

        return (
            "당신은 KOSPI 멀티에이전트 트레이딩 시스템의 최종 의사결정 에이전트입니다.\n"
            "아래 신호들을 종합하여 현재 포트폴리오 변경을 승인할지 거부할지 결정하세요.\n\n"
            f"에이전트 신호 (최근 {len(agent_signals)}건):\n{signals_str}\n\n"
            f"리스크 경고 ({len(risk_warnings)}건):\n{risk_str}"
            f"{debate_str}\n\n"
            "규칙: 비중 변경 불가. approve 또는 veto만 결정.\n"
            "반환 가능 reason_code: "
            "NORMAL_APPROVE / NEWS_DIVERGENCE / RISK_FAST_TRIGGER / DEBATE_CONFLICT / QUANT_ANOMALY (5종). "
            "TIMEOUT과 MISSING_PORTFOLIO_PATCH는 LLM이 직접 반환하지 않음.\n\n"
            "JSON으로 응답: "
            '{"approved": true/false, "reason_code": "...", '
            '"veto_reason": "null 또는 한국어", "confidence": 0.0~1.0}'
        )

    def _parse_cold_llm(self, content: str) -> dict[str, Any]:
        """FDA Cold Path LLM 응답 파싱."""
        try:
            parsed = parse_llm_json(content)
            if not isinstance(parsed, dict):
                raise TypeError(f"LLM JSON root must be object. got={type(parsed)}")
            approved = self._safe_bool(parsed.get("approved"), default=False)
            rc = str(parsed.get("reason_code", "")).strip()
            if rc not in self._valid_reason_codes:
                logger.warning(
                    "[fda] Cold Path LLM reason_code 오류. NEWS_DIVERGENCE fail-closed: %s",
                    rc,
                )
                return {
                    "approved": False,
                    "reason_code": "NEWS_DIVERGENCE",
                    "veto_reason": "Cold Path LLM reason_code 오류. FDA fail-closed.",
                    "confidence": parsed.get("confidence"),
                }
            return {
                "approved": approved,
                "reason_code": rc,
                "veto_reason": parsed.get("veto_reason"),
                "confidence": parsed.get("confidence"),
            }
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("[fda] Cold Path LLM 응답 파싱 실패. NEWS_DIVERGENCE fail-closed: %s", e)
            return {
                "approved": False,
                "reason_code": "NEWS_DIVERGENCE",
                "veto_reason": "Cold Path LLM 응답 파싱 실패. FDA fail-closed.",
            }

    @staticmethod
    def _safe_bool(value: Any, default: bool = True) -> bool:
        """LLM bool-like 값을 문자열까지 안전하게 해석한다."""
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "y", "1", "approve", "approved", "승인"}:
                return True
            if normalized in {"false", "no", "n", "0", "veto", "reject", "rejected", "거부"}:
                return False
        return default

    @staticmethod
    def _safe_confidence(value: Any, default: float = 0.7) -> float:
        """FDA confidence를 finite 0.0~1.0으로 정규화한다."""
        try:
            x = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(x):
            return default
        return max(0.0, min(1.0, x))

    # ================================================================== #
    # AgentBase API
    # ================================================================== #

    def report(self, report_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """FDA publishes: final_decision (단일). AgentBase ALLOWED_PUBLISH_CHANNELS 검증."""
        if report_type not in self.ALLOWED_PUBLISH_CHANNELS:
            raise ValueError(
                f"FDAAgent.report: invalid report_type={report_type}. "
                f"허용: {sorted(self.ALLOWED_PUBLISH_CHANNELS)}"
            )
        return {
            "report_type": report_type,
            "agent": "FDAAgent",
            "payload": payload,
        }
